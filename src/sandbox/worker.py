from __future__ import annotations

import array
import builtins
import contextlib
import io
import json
import os
import signal
import socket
import struct
import sys
import traceback
from collections.abc import Callable
from typing import Any

from sandbox.isolate import IsolationError, clamp_nproc, isolate_self

os.environ.pop("SANDBOX_SECRET", None)
os.environ.setdefault("MPLBACKEND", "Agg")

WORKSPACE = os.path.realpath(os.environ["SANDBOX_WORKSPACE"])
VIRTUAL_ROOT = "/workspace"

_globals: dict[str, object] = {"__name__": "__main__"}
MAX_CAPTURE = 1_000_000
_MAX_MSG = 2_500_000
_real_getcwd = os.getcwd
_real_realpath = os.path.realpath


def _as_text(path: object) -> str:
    if isinstance(path, bytes):
        return os.fsdecode(path)
    return os.fspath(path)  # type: ignore[arg-type]


def _map_path(path: object) -> object:
    if isinstance(path, int):
        return path
    if not isinstance(path, (str, bytes, os.PathLike)):
        return path
    raw = _as_text(path)
    if raw != VIRTUAL_ROOT and not raw.startswith(f"{VIRTUAL_ROOT}/"):
        return path
    rest = raw[len(VIRTUAL_ROOT) :].lstrip("/")
    mapped = os.path.join(WORKSPACE, rest) if rest else WORKSPACE
    real = _real_realpath(mapped)
    if real != WORKSPACE and not real.startswith(WORKSPACE + os.sep):
        raise PermissionError("path escapes workspace")
    return os.fsencode(real) if isinstance(path, bytes) else real


def _wrap_path(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(path: object, *args: object, **kwargs: object) -> Any:
        return fn(_map_path(path), *args, **kwargs)

    wrapped.__name__ = getattr(fn, "__name__", "wrapped")
    return wrapped


def _wrap_two_paths(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(src: object, dst: object, *args: object, **kwargs: object) -> Any:
        return fn(_map_path(src), _map_path(dst), *args, **kwargs)

    wrapped.__name__ = getattr(fn, "__name__", "wrapped")
    return wrapped


def _install_workspace_alias() -> None:
    os.stat = _wrap_path(os.stat)  # type: ignore[method-assign]
    os.lstat = _wrap_path(os.lstat)  # type: ignore[method-assign]
    os.access = _wrap_path(os.access)  # type: ignore[method-assign]
    os.mkdir = _wrap_path(os.mkdir)  # type: ignore[method-assign]
    os.makedirs = _wrap_path(os.makedirs)  # type: ignore[method-assign]
    os.listdir = _wrap_path(os.listdir)  # type: ignore[method-assign]
    os.scandir = _wrap_path(os.scandir)  # type: ignore[method-assign]
    os.remove = _wrap_path(os.remove)  # type: ignore[method-assign]
    os.unlink = _wrap_path(os.unlink)  # type: ignore[method-assign]
    os.rmdir = _wrap_path(os.rmdir)  # type: ignore[method-assign]
    os.chmod = _wrap_path(os.chmod)  # type: ignore[method-assign]
    os.rename = _wrap_two_paths(os.rename)  # type: ignore[method-assign]
    os.replace = _wrap_two_paths(os.replace)  # type: ignore[method-assign]
    os.chdir = _wrap_path(os.chdir)  # type: ignore[method-assign]
    os.open = _wrap_path(os.open)  # type: ignore[method-assign]

    def getcwd() -> str:
        real = _real_getcwd()
        if real == WORKSPACE:
            return VIRTUAL_ROOT
        if real.startswith(WORKSPACE + os.sep):
            return VIRTUAL_ROOT + real[len(WORKSPACE) :]
        return real

    os.getcwd = getcwd  # type: ignore[method-assign]
    real_open = io.open

    def mapped_open(file: object, *args: object, **kwargs: object) -> Any:
        if not isinstance(file, int):
            file = _map_path(file)
        return real_open(file, *args, **kwargs)

    io.open = mapped_open  # type: ignore[assignment]
    builtins.open = mapped_open  # type: ignore[assignment]


_install_workspace_alias()


def _trim(value: str) -> str:
    if len(value) <= MAX_CAPTURE:
        return value
    return value[-MAX_CAPTURE:]


def _exec(code: str) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(code, _globals)  # noqa: S102 — intentional sandbox eval
    except SystemExit as error:
        code_value = error.code
        exit_code = code_value if isinstance(code_value, int) else 1
        if exit_code != 0 and code_value is not None:
            stderr.write(str(code_value))
    except Exception:  # noqa: BLE001 — surface user code errors
        stderr.write(traceback.format_exc())
        exit_code = 1
    return {
        "stdout": _trim(stdout.getvalue()),
        "stderr": _trim(stderr.getvalue()),
        "exit_code": exit_code,
    }


def _send(result_fd: int, payload: dict[str, object]) -> None:
    data = (json.dumps(payload) + "\n").encode("utf-8")
    view = memoryview(data)
    while len(view) > 0:
        written = os.write(result_fd, view)
        view = view[written:]


def _recvall(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        piece = sock.recv(size - len(chunks))
        if not piece:
            raise OSError("kernel socket closed")
        chunks.extend(piece)
    return bytes(chunks)


def _send_msg(sock: socket.socket, payload: dict[str, object]) -> None:
    data = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_msg(sock: socket.socket) -> dict[str, object]:
    header = _recvall(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length > _MAX_MSG:
        raise OSError("kernel message too large")
    body = json.loads(_recvall(sock, length).decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("invalid kernel message")
    return body


def _send_fd(sock: socket.socket, fd: int) -> None:
    encoded = array.array("i", [fd])
    sock.sendmsg([b"\x01"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, encoded)])


def _recv_fd(sock: socket.socket) -> int:
    _data, anc, _flags, _addr = sock.recvmsg(1, socket.CMSG_LEN(array.array("i").itemsize))
    for level, typ, cmsg_data in anc:
        if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
            return array.array("i", cmsg_data[: array.array("i").itemsize])[0]
    raise OSError("missing handed-off fd")


def _reap_children(_signum: int = 0, _frame: object = None) -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _kernel_main(conn: socket.socket, *, apply_isolation: bool) -> None:
    if apply_isolation:
        isolate_self(WORKSPACE)
        os.chdir(WORKSPACE)
    while True:
        message = _recv_msg(conn)
        command = message.get("cmd")
        if command == "ping":
            _send_msg(conn, {"ok": True})
            continue
        if command != "exec":
            _send_msg(conn, {"ok": False, "error": "unknown command"})
            continue
        _kernel_exec(conn, str(message.get("code", "")))


def _kernel_exec(conn: socket.socket, code: str) -> None:
    local, remote = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    pid = os.fork()
    if pid == 0:
        conn.close()
        local.close()
        _exec_child(remote, code)
        os._exit(0)
    remote.close()
    os.waitpid(pid, 0)
    result = _recv_msg(local)
    _send_msg(conn, result)
    _send_fd(local, conn.fileno())
    conn.close()
    local.close()
    os._exit(0)


def _exec_child(remote: socket.socket, code: str) -> None:
    clamp_nproc(1)
    result = _exec(code)
    clamp_nproc(256)
    try:
        pid = os.fork()
    except OSError:
        _send_msg(remote, {"ok": True, **result})
        return
    if pid != 0:
        os._exit(0)
    _send_msg(remote, {"ok": True, **result})
    handed = _recv_fd(remote)
    remote.close()
    conn = socket.socket(fileno=handed)
    _kernel_main(conn, apply_isolation=False)


def _supervisor_main(conn: socket.socket, result_fd: int) -> None:
    signal.signal(signal.SIGCHLD, _reap_children)
    for raw in sys.stdin:
        line = raw.strip()
        if len(line) == 0:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _send(result_fd, {"ok": False, "error": "invalid json", "nonce": ""})
            continue
        nonce = str(message.get("nonce", ""))
        command = message.get("cmd")
        kernel_msg: dict[str, object]
        if command == "ping":
            kernel_msg = {"cmd": "ping"}
        elif command == "exec":
            kernel_msg = {"cmd": "exec", "code": str(message.get("code", ""))}
        else:
            _send(result_fd, {"ok": False, "error": "unknown command", "nonce": nonce})
            continue
        try:
            _send_msg(conn, kernel_msg)
            body = _recv_msg(conn)
        except (OSError, ValueError, json.JSONDecodeError):
            _send(result_fd, {"ok": False, "error": "kernel exited", "nonce": nonce})
            return
        body["nonce"] = nonce
        _send(result_fd, body)


def main() -> None:
    raw_fd = os.environ.pop("SANDBOX_RESULT_FD", None)
    if raw_fd is None:
        raise SystemExit("SANDBOX_RESULT_FD is required")
    result_fd = int(raw_fd)
    supervisor, kernel = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    pid = os.fork()
    if pid == 0:
        os.close(result_fd)
        supervisor.close()
        try:
            sys.stdin.close()
        except OSError:
            pass
        try:
            _kernel_main(kernel, apply_isolation=True)
        except IsolationError as error:
            with contextlib.suppress(OSError):
                _send_msg(kernel, {"ok": False, "error": str(error)})
            os._exit(1)
        except Exception:  # noqa: BLE001 — kernel startup must not fall through
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    kernel.close()
    _supervisor_main(supervisor, result_fd)


if __name__ == "__main__":
    main()
