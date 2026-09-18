from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, NoReturn, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from sandbox.call_log import configure_logger, logged_call
from sandbox.ops import (
    DEFAULT_TIMEOUT_MS,
    MAX_CODE_CHARS,
    MAX_TIMEOUT_MS,
    MAX_WRITE_CHARS,
    MIN_TIMEOUT_MS,
    SandboxOpError,
    delete_session as delete_session_op,
    execute as execute_op,
    get_file as get_file_op,
    get_session,
    put_file,
)

T = TypeVar("T")

logger = configure_logger("sandbox.mcp")

INSTRUCTIONS = """\
Python code-execution sandbox. Each session_id is a persistent kernel + /workspace.
cwd is /workspace. Put uploads under /workspace/uploads/. Write outputs into /workspace.
Python has no pip. Installed packages: openpyxl, python-docx,
reportlab, python-pptx, pandas, pypandoc, numpy, matplotlib.
The backend chooses session_id (typically userId_chatId). After execute, harvest file
bytes with read_file — do not treat workspace paths as public URLs.
"""

mcp = MCPServer(
    "python-mcp-sandbox",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)


def _raise(error: SandboxOpError) -> NoReturn:
    raise ToolError(error.detail) from error


async def _logged(
    tool: str,
    session_id: str,
    op: Callable[[], Awaitable[T]],
    **extra: object,
) -> T:
    try:
        return await logged_call(
            logger,
            "mcp",
            tool,
            session_id,
            op,
            anticipated=(ToolError,),
            **extra,
        )
    except SandboxOpError as error:
        _raise(error)


@mcp.tool(
    title="Execute Python",
    annotations=ToolAnnotations(open_world_hint=False),
)
async def execute(
    session_id: Annotated[
        str,
        Field(description="Backend-chosen session id. Regex [A-Za-z0-9._-]{1,200}."),
    ],
    code: Annotated[
        str,
        Field(min_length=1, max_length=MAX_CODE_CHARS, description="Python source to exec."),
    ],
    timeout_ms: Annotated[
        int,
        Field(ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS),
    ] = DEFAULT_TIMEOUT_MS,
) -> dict[str, object]:
    """Run Python in a persistent session. Variables and files survive until delete_session or idle reap.

    Returns exitCode, stdout, stderr, timedOut, and files created/changed by this run
    (relative paths, no __pycache__)."""
    return await _logged(
        "execute",
        session_id,
        lambda: execute_op(session_id, code, timeout_ms),
        code_chars=len(code),
        timeout_ms=timeout_ms,
    )


@mcp.tool(
    title="Write workspace file",
    annotations=ToolAnnotations(open_world_hint=False, idempotent_hint=True),
)
async def write_file(
    session_id: Annotated[str, Field(description="Backend-chosen session id.")],
    path: Annotated[
        str,
        Field(description="Relative path inside the session workspace, e.g. uploads/input.csv."),
    ],
    content: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_WRITE_CHARS,
            description="File bytes as utf-8 text or base64.",
        ),
    ],
    encoding: Literal["utf-8", "base64"] = "utf-8",
    mime: str = "application/octet-stream",
) -> dict[str, object]:
    """Write a file into the session workspace. Relative path only — no `..`."""

    async def op() -> dict[str, object]:
        try:
            body = content.encode("utf-8") if encoding == "utf-8" else base64.b64decode(content)
        except (ValueError, UnicodeError) as error:
            raise ToolError("invalid content") from error
        return await put_file(session_id, path, body, mime)

    return await _logged(
        "write_file",
        session_id,
        op,
        path=path,
        encoding=encoding,
        content_chars=len(content),
    )


@mcp.tool(
    title="Read workspace file",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True),
)
async def read_file(
    session_id: Annotated[str, Field(description="Backend-chosen session id.")],
    path: Annotated[str, Field(description="Relative path inside the session workspace.")],
    encoding: Literal["utf-8", "base64"] = "base64",
) -> dict[str, object]:
    """Read a workspace file. Default content encoding is base64 (safe for binary)."""

    async def op() -> dict[str, object]:
        data, mime = await get_file_op(session_id, path)
        if encoding == "utf-8":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ToolError("file is not valid utf-8; use encoding=base64") from error
            content: str = text
        else:
            content = base64.b64encode(data).decode("ascii")
        return {
            "path": path,
            "size": len(data),
            "mime": mime,
            "encoding": encoding,
            "content": content,
        }

    return await _logged("read_file", session_id, op, path=path, encoding=encoding)


@mcp.tool(
    title="List workspace files",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True),
)
async def list_files(
    session_id: Annotated[str, Field(description="Backend-chosen session id.")],
) -> dict[str, object]:
    """List current workspace files and whether the Python kernel is alive."""
    return await _logged("list_files", session_id, lambda: get_session(session_id))


@mcp.tool(
    title="Delete session",
    annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False),
)
async def delete_session(
    session_id: Annotated[str, Field(description="Backend-chosen session id.")],
) -> dict[str, bool]:
    """Kill the kernel and wipe the workspace for this session."""
    return await _logged("delete_session", session_id, lambda: delete_session_op(session_id))


def make_mcp_app():
    return mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
        max_request_body_size=32 * 1024 * 1024,
        stateless_http=True,
    )
