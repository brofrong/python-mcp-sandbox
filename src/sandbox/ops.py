from __future__ import annotations

from sandbox.paths import (
    MAX_FILE_BYTES,
    MAX_WORKSPACE_BYTES,
    SESSION_ID_RE,
    list_files,
    mime_for,
    resolve_relative,
    workspace_dir,
    workspace_size,
)
from sandbox.sessions import manager

MAX_CODE_CHARS = 200_000
MIN_TIMEOUT_MS = 1
MAX_TIMEOUT_MS = 120_000
DEFAULT_TIMEOUT_MS = 30_000


class SandboxOpError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def require_session_id(session_id: str) -> str:
    if SESSION_ID_RE.fullmatch(session_id) is None:
        raise SandboxOpError(400, "invalid session id")
    return session_id


async def get_session(session_id: str) -> dict[str, object]:
    sid = require_session_id(session_id)
    workspace = workspace_dir(sid, create=False)
    return {"files": list_files(workspace), "alive": await manager.is_alive(sid)}


async def put_file(
    session_id: str,
    file_path: str,
    body: bytes,
    content_type: str | None,
) -> dict[str, object]:
    sid = require_session_id(session_id)
    session = await manager.get(sid)
    try:
        dest = resolve_relative(session.workspace, file_path)
    except ValueError as error:
        raise SandboxOpError(400, "invalid path") from error
    if len(body) == 0:
        raise SandboxOpError(400, "empty file")
    if len(body) > MAX_FILE_BYTES:
        raise SandboxOpError(413, "file too large")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    if workspace_size(session.workspace) > MAX_WORKSPACE_BYTES:
        dest.unlink(missing_ok=True)
        raise SandboxOpError(413, "workspace too large")
    mime = mime_for(dest, fallback=content_type or "application/octet-stream")
    return {
        "path": dest.relative_to(session.workspace).as_posix(),
        "size": len(body),
        "mime": mime,
    }


async def get_file(session_id: str, file_path: str) -> tuple[bytes, str]:
    sid = require_session_id(session_id)
    workspace = workspace_dir(sid, create=False)
    try:
        dest = resolve_relative(workspace, file_path)
    except ValueError as error:
        raise SandboxOpError(400, "invalid path") from error
    if not dest.is_file():
        raise SandboxOpError(404, "not found")
    return dest.read_bytes(), mime_for(dest)


async def execute(session_id: str, code: str, timeout_ms: int) -> dict[str, object]:
    sid = require_session_id(session_id)
    if len(code) < 1 or len(code) > MAX_CODE_CHARS:
        raise SandboxOpError(400, "invalid code")
    if timeout_ms < MIN_TIMEOUT_MS or timeout_ms > MAX_TIMEOUT_MS:
        raise SandboxOpError(400, "invalid timeout")
    return await manager.execute(sid, code, timeout_ms)


async def delete_session(session_id: str) -> dict[str, bool]:
    sid = require_session_id(session_id)
    await manager.delete(sid, delete_workspace=True)
    return {"ok": True}
