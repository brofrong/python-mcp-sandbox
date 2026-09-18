from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

_SAFE_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LANGUAGE",
    "TZ",
    "LD_LIBRARY_PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "PYTHONHOME",
)

DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
DEFAULT_CPU_SECONDS = 300
DEFAULT_NPROC = 256

# landlock syscalls are stable across x86_64 / arm64 / riscv64
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13
_FS_TRUNCATE = 1 << 14
_FS_IOCTL_DEV = 1 << 15
_NET_BIND_TCP = 1 << 0
_NET_CONNECT_TCP = 1 << 1


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _PathBeneath(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class IsolationError(Exception):
    pass


def isolation_required() -> bool:
    """Fail closed only when explicitly requested.

    Docker sets SANDBOX_REQUIRE_ISOLATION=1. Unit tests and a native
    Linux run (GitHub Actions, local uvicorn) often cannot unshare a
    user/net namespace, so the default is best-effort isolation.
    """
    flag = os.environ.get("SANDBOX_REQUIRE_ISOLATION")
    if flag is None:
        return False
    return flag.strip().lower() not in {"", "0", "false", "no"}


def worker_env(*, workspace: str, pythonpath: str, result_fd: int) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _SAFE_ENV:
        value = os.environ.get(key)
        if value:
            env[key] = value
    tmp = Path(workspace) / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": workspace,
            "TMPDIR": str(tmp),
            "SANDBOX_WORKSPACE": workspace,
            "SANDBOX_RESULT_FD": str(result_fd),
            "MPLBACKEND": "Agg",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": pythonpath,
            "SANDBOX_MEMORY_BYTES": os.environ.get(
                "SANDBOX_MEMORY_BYTES",
                str(DEFAULT_MEMORY_BYTES),
            ),
            "SANDBOX_CPU_SECONDS": os.environ.get(
                "SANDBOX_CPU_SECONDS",
                str(DEFAULT_CPU_SECONDS),
            ),
        }
    )
    required = os.environ.get("SANDBOX_REQUIRE_ISOLATION")
    if required:
        env["SANDBOX_REQUIRE_ISOLATION"] = required
    return env


def isolate_self(workspace: str) -> None:
    os.environ.pop("SANDBOX_SECRET", None)
    required = isolation_required()
    if sys.platform == "linux":
        if not _unshare_net() and required:
            raise IsolationError("network unshare failed")
        # Landlock paths are the Docker jail layout. Applying them on a
        # GitHub Actions / native host hides /proc and misses toolcache
        # paths, so only enforce that filesystem policy when required.
        if required and not _landlock(workspace):
            raise IsolationError("landlock failed")
    elif required:
        raise IsolationError("isolation required but platform is not linux")
    _apply_rlimits()


def _apply_rlimits() -> None:
    try:
        import resource

        memory = int(os.environ.get("SANDBOX_MEMORY_BYTES", str(DEFAULT_MEMORY_BYTES)))
        cpu = int(os.environ.get("SANDBOX_CPU_SECONDS", str(DEFAULT_CPU_SECONDS)))
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_NPROC, (DEFAULT_NPROC, DEFAULT_NPROC))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        return


def _unshare_net() -> bool:
    newuser = getattr(os, "CLONE_NEWUSER", 0x10000000)
    newnet = getattr(os, "CLONE_NEWNET", 0x40000000)
    uid = os.getuid()
    gid = os.getgid()
    try:
        os.unshare(newuser | newnet)
    except (AttributeError, OSError):
        return False
    try:
        Path("/proc/self/setgroups").write_text("deny")
        Path("/proc/self/uid_map").write_text(f"{uid} {uid} 1")
        Path("/proc/self/gid_map").write_text(f"{gid} {gid} 1")
    except OSError:
        return False
    return True


def _landlock(workspace: str) -> bool:
    libc_name = ctypes.util.find_library("c") or "c"
    libc = ctypes.CDLL(libc_name, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
        ctypes.c_void_p(0),
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if abi < 1:
        return False

    handled_fs = (
        _FS_EXECUTE
        | _FS_WRITE_FILE
        | _FS_READ_FILE
        | _FS_READ_DIR
        | _FS_REMOVE_DIR
        | _FS_REMOVE_FILE
        | _FS_MAKE_DIR
        | _FS_MAKE_REG
        | _FS_MAKE_SOCK
        | _FS_MAKE_FIFO
        | _FS_MAKE_SYM
    )
    if abi >= 2:
        handled_fs |= _FS_REFER
    if abi >= 3:
        handled_fs |= _FS_TRUNCATE
    if abi >= 5:
        handled_fs |= _FS_IOCTL_DEV
    handled_net = 0
    if abi >= 4:
        handled_net = _NET_BIND_TCP | _NET_CONNECT_TCP

    attr = _RulesetAttr(handled_fs, handled_net)
    ruleset = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr) if abi >= 4 else 8),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        return False

    ro = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
    if abi >= 5:
        ro |= _FS_IOCTL_DEV
    rw = (
        ro
        | _FS_WRITE_FILE
        | _FS_REMOVE_DIR
        | _FS_REMOVE_FILE
        | _FS_MAKE_DIR
        | _FS_MAKE_REG
        | _FS_MAKE_FIFO
        | _FS_MAKE_SYM
    )
    if abi >= 2:
        rw |= _FS_REFER
    if abi >= 3:
        rw |= _FS_TRUNCATE

    ro_paths = [
        "/usr",
        "/lib",
        "/lib64",
        "/bin",
        "/sbin",
        "/app",
        "/etc",
        "/dev",
        "/run",
        "/proc/cpuinfo",
        "/proc/meminfo",
        "/sys/devices/system/cpu",
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
    ]
    rw_paths = [workspace, str(Path(workspace) / ".tmp")]
    try:
        for path in ro_paths:
            _landlock_path(libc, int(ruleset), path, ro & handled_fs)
        for path in rw_paths:
            _landlock_path(libc, int(ruleset), path, rw & handled_fs)
        libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        rc = libc.syscall(
            ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
            ctypes.c_int(int(ruleset)),
            ctypes.c_uint32(0),
        )
        return rc >= 0
    finally:
        os.close(int(ruleset))


def _landlock_path(libc: ctypes.CDLL, ruleset: int, path: str, access: int) -> None:
    if not path or not os.path.exists(path):
        return
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = _PathBeneath(access, fd)
        libc.syscall(
            ctypes.c_long(_SYS_LANDLOCK_ADD_RULE),
            ctypes.c_int(ruleset),
            ctypes.c_uint(_LANDLOCK_RULE_PATH_BENEATH),
            ctypes.byref(rule),
            ctypes.c_uint32(0),
        )
    finally:
        os.close(fd)
