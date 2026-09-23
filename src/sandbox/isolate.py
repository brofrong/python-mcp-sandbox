from __future__ import annotations

import ctypes
import errno
import os
import stat
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

# Linux uapi. Same numbers on x86_64 and aarch64.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12
_ACCESS_FS_REFER = 1 << 13
_ACCESS_FS_TRUNCATE = 1 << 14
_ACCESS_FS_IOCTL_DEV = 1 << 15

_ABI1 = (
    _ACCESS_FS_EXECUTE
    | _ACCESS_FS_WRITE_FILE
    | _ACCESS_FS_READ_FILE
    | _ACCESS_FS_READ_DIR
    | _ACCESS_FS_REMOVE_DIR
    | _ACCESS_FS_REMOVE_FILE
    | _ACCESS_FS_MAKE_CHAR
    | _ACCESS_FS_MAKE_DIR
    | _ACCESS_FS_MAKE_REG
    | _ACCESS_FS_MAKE_SOCK
    | _ACCESS_FS_MAKE_FIFO
    | _ACCESS_FS_MAKE_BLOCK
    | _ACCESS_FS_MAKE_SYM
)
_FILE_ACCESS = (
    _ACCESS_FS_EXECUTE
    | _ACCESS_FS_WRITE_FILE
    | _ACCESS_FS_READ_FILE
    | _ACCESS_FS_TRUNCATE
    | _ACCESS_FS_IOCTL_DEV
)
_READ_ONLY = _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR

_OPTIONAL_RO_DIRS = ("/usr", "/lib", "/lib64", "/etc")
_OPTIONAL_RO_FILES = ("/proc/cpuinfo", "/proc/meminfo")
_OPTIONAL_RW_FILES = ("/dev/null", "/dev/urandom")
_OPTIONAL_RO_PROC_DIRS = ("/proc/self", "/proc/thread-self")


class IsolationError(RuntimeError):
    pass


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


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
        }
    )
    return env


def restrict_to_workspace(workspace: str) -> None:
    """Landlock the kernel process on Linux. No-op on other platforms."""
    if sys.platform != "linux":
        return
    try:
        _apply_landlock(workspace)
    except OSError as error:
        raise IsolationError(f"landlock failed: {error}") from error


def _linux_libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    return libc


def _bind(libc: ctypes.CDLL, *argtypes: type[ctypes._CData]) -> ctypes._CFuncPtr:
    prototype = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_long, *argtypes, use_errno=True)
    return prototype(("syscall", libc))


def _prctl(libc: ctypes.CDLL, option: int, arg2: int, arg3: int, arg4: int, arg5: int) -> int:
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    return int(prctl(option, arg2, arg3, arg4, arg5))


def _check(result: int) -> int:
    if result >= 0:
        return int(result)
    err = ctypes.get_errno() or errno.EINVAL
    raise OSError(err, os.strerror(err))


def _handled_access(abi: int) -> int:
    access = _ABI1
    if abi >= 2:
        access |= _ACCESS_FS_REFER
    if abi >= 3:
        access |= _ACCESS_FS_TRUNCATE
    if abi >= 4:
        access |= _ACCESS_FS_IOCTL_DEV
    return access


def _apply_landlock(workspace: str) -> None:
    libc = _linux_libc()
    create_ruleset = _bind(libc, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint)
    add_rule = _bind(libc, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    restrict_self = _bind(libc, ctypes.c_int, ctypes.c_uint)
    ctypes.set_errno(0)
    abi = _check(
        create_ruleset(
            _SYS_LANDLOCK_CREATE_RULESET,
            None,
            0,
            _LANDLOCK_CREATE_RULESET_VERSION,
        )
    )
    if abi < 1:
        raise IsolationError("landlock ABI is unavailable")
    handled = _handled_access(abi)
    attr = _RulesetAttr(handled_access_fs=handled)
    ctypes.set_errno(0)
    ruleset = _check(
        create_ruleset(
            _SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.sizeof(attr),
            0,
        )
    )
    try:
        seen: set[str] = set()
        root = os.path.realpath(workspace)
        if not os.path.isdir(root):
            raise IsolationError("workspace is not a directory")
        _add_rule(add_rule, ruleset, root, handled, handled=handled, seen=seen)
        read_only = _READ_ONLY & handled
        for path in (*_OPTIONAL_RO_DIRS, *_runtime_read_dirs()):
            _add_optional(add_rule, ruleset, path, read_only, handled=handled, seen=seen)
        for path in _OPTIONAL_RO_PROC_DIRS:
            _add_optional(add_rule, ruleset, path, read_only, handled=handled, seen=seen)
        for path in _OPTIONAL_RO_FILES:
            _add_optional(add_rule, ruleset, path, read_only, handled=handled, seen=seen)
        device = (_FILE_ACCESS & handled) & ~_ACCESS_FS_EXECUTE
        for path in _OPTIONAL_RW_FILES:
            _add_optional(add_rule, ruleset, path, device, handled=handled, seen=seen)
        ctypes.set_errno(0)
        if _prctl(libc, _PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            err = ctypes.get_errno() or errno.EPERM
            raise OSError(err, os.strerror(err))
        ctypes.set_errno(0)
        _check(restrict_self(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0))
    finally:
        os.close(ruleset)


def _runtime_read_dirs() -> list[str]:
    paths = [sys.prefix, sys.base_prefix, str(Path(__file__).resolve().parent.parent)]
    return [path for path in paths if path]


def _add_optional(
    add_rule: ctypes._CFuncPtr,
    ruleset: int,
    path: str,
    access: int,
    *,
    handled: int,
    seen: set[str],
) -> None:
    if not os.path.exists(path):
        return
    _add_rule(add_rule, ruleset, os.path.realpath(path), access, handled=handled, seen=seen)


def _add_rule(
    add_rule: ctypes._CFuncPtr,
    ruleset: int,
    path: str,
    access: int,
    *,
    handled: int,
    seen: set[str],
) -> None:
    if path in seen:
        return
    seen.add(path)
    is_dir = stat.S_ISDIR(os.stat(path).st_mode)
    allowed = access & handled
    if not is_dir:
        allowed &= _FILE_ACCESS
    if allowed == 0:
        return
    flags = getattr(os, "O_PATH", 0) | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        beneath = _PathBeneathAttr(allowed_access=allowed, parent_fd=fd)
        ctypes.set_errno(0)
        _check(
            add_rule(
                _SYS_LANDLOCK_ADD_RULE,
                ruleset,
                _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(beneath),
                0,
            )
        )
    finally:
        os.close(fd)
