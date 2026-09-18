from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import sys
import traceback
from collections.abc import Callable
from typing import Any

from sandbox.isolate import isolate_self

os.environ.pop("SANDBOX_SECRET", None)
os.environ.setdefault("MPLBACKEND", "Agg")

WORKSPACE = os.path.realpath(os.environ["SANDBOX_WORKSPACE"])
VIRTUAL_ROOT = "/workspace"
isolate_self(WORKSPACE)
os.chdir(WORKSPACE)

_globals: dict[str, object] = {"__name__": "__main__"}
MAX_CAPTURE = 1_000_000
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


def main() -> None:
    raw_fd = os.environ.get("SANDBOX_RESULT_FD")
    if raw_fd is None:
        raise SystemExit("SANDBOX_RESULT_FD is required")
    result_fd = int(raw_fd)
    for raw in sys.stdin:
        line = raw.strip()
        if len(line) == 0:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _send(result_fd, {"ok": False, "error": "invalid json", "nonce": ""})
            continue
        command = message.get("cmd")
        nonce = str(message.get("nonce", ""))
        if command == "ping":
            _send(result_fd, {"ok": True, "nonce": nonce})
            continue
        if command == "exec":
            result = _exec(str(message.get("code", "")))
            _send(result_fd, {"ok": True, "nonce": nonce, **result})
            continue
        _send(result_fd, {"ok": False, "error": "unknown command", "nonce": nonce})


if __name__ == "__main__":
    main()
