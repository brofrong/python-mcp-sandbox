from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from sandbox.mcp_server import make_mcp_app, mcp
from sandbox.ops import (
    DEFAULT_TIMEOUT_MS,
    MAX_CODE_CHARS,
    MAX_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    SandboxOpError,
    delete_session as delete_session_op,
    execute as execute_op,
    get_file as get_file_op,
    get_session,
    put_file,
)

mcp_asgi = make_mcp_app()


class ExecuteBody(BaseModel):
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    timeoutMs: int = Field(default=DEFAULT_TIMEOUT_MS, ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS)


def require_secret(authorization: str | None) -> None:
    expected = os.environ.get("SANDBOX_SECRET", "")
    if len(expected) == 0:
        raise HTTPException(status_code=500, detail="SANDBOX_SECRET is not set")
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="unauthorized")


def raise_op(error: SandboxOpError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


async def _reap_loop() -> None:
    from sandbox.sessions import manager

    while True:
        await asyncio.sleep(60)
        with suppress(Exception):
            await manager.reap()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if len(os.environ.get("SANDBOX_SECRET", "")) == 0:
        raise RuntimeError("SANDBOX_SECRET is required")
    from sandbox.sessions import manager

    reap_task = asyncio.create_task(_reap_loop())
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        reap_task.cancel()
        with suppress(asyncio.CancelledError):
            await reap_task
        await manager.close()


app = FastAPI(title="python-mcp-sandbox", version="0.1.0", lifespan=lifespan)
app.mount("/mcp", mcp_asgi)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/v1/sessions/{session_id}")
async def get_session_route(
    session_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_secret(authorization)
    try:
        return await get_session(session_id)
    except SandboxOpError as error:
        raise_op(error)
        raise


@app.put("/v1/sessions/{session_id}/files/{file_path:path}")
async def put_file_route(
    session_id: str,
    file_path: str,
    request: Request,
    authorization: str | None = Header(default=None),
    content_type: str | None = Header(default=None, alias="Content-Type"),
) -> dict[str, object]:
    require_secret(authorization)
    body = await request.body()
    try:
        return await put_file(session_id, file_path, body, content_type)
    except SandboxOpError as error:
        raise_op(error)
        raise


@app.get("/v1/sessions/{session_id}/files/{file_path:path}")
async def get_file_route(
    session_id: str,
    file_path: str,
    authorization: str | None = Header(default=None),
) -> Response:
    require_secret(authorization)
    try:
        data, mime = await get_file_op(session_id, file_path)
    except SandboxOpError as error:
        raise_op(error)
        raise
    return Response(content=data, media_type=mime)


@app.post("/v1/sessions/{session_id}/execute")
async def execute_route(
    session_id: str,
    body: ExecuteBody,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    require_secret(authorization)
    try:
        return await execute_op(session_id, body.code, body.timeoutMs)
    except SandboxOpError as error:
        raise_op(error)
        raise


@app.delete("/v1/sessions/{session_id}")
async def delete_session_route(
    session_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, bool]:
    require_secret(authorization)
    try:
        return await delete_session_op(session_id)
    except SandboxOpError as error:
        raise_op(error)
        raise
