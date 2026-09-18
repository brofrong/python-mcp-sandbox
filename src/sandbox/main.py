from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import NoReturn

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from sandbox.auth_asgi import BearerAuthASGI, init_bearer_secret
from sandbox.call_log import configure_logger, logged_call
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
from sandbox.paths import MAX_FILE_BYTES

logger = configure_logger("sandbox.api")
mcp_asgi = make_mcp_app()


class ExecuteBody(BaseModel):
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    timeoutMs: int = Field(default=DEFAULT_TIMEOUT_MS, ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS)


def raise_op(error: SandboxOpError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


async def read_body_capped(request: Request, cap: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > cap:
                raise SandboxOpError(413, "file too large")
        except ValueError as error:
            raise SandboxOpError(400, "invalid content-length") from error
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise SandboxOpError(413, "file too large")
        chunks.append(chunk)
    return b"".join(chunks)


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
    init_bearer_secret()
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


fastapi_app = FastAPI(
    title="openrouter-sandbox",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
fastapi_app.mount("/mcp", mcp_asgi)


@fastapi_app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@fastapi_app.get("/v1/sessions/{session_id}")
async def get_session_route(session_id: str) -> dict[str, object]:
    try:
        return await logged_call(
            logger,
            "api",
            "list_files",
            session_id,
            lambda: get_session(session_id),
        )
    except SandboxOpError as error:
        raise_op(error)


@fastapi_app.put("/v1/sessions/{session_id}/files/{file_path:path}")
async def put_file_route(
    session_id: str,
    file_path: str,
    request: Request,
    content_type: str | None = Header(default=None, alias="Content-Type"),
) -> dict[str, object]:
    async def op() -> dict[str, object]:
        body = await read_body_capped(request, MAX_FILE_BYTES)
        return await put_file(session_id, file_path, body, content_type)

    try:
        return await logged_call(
            logger,
            "api",
            "write_file",
            session_id,
            op,
            path=file_path,
        )
    except SandboxOpError as error:
        raise_op(error)


@fastapi_app.get("/v1/sessions/{session_id}/files/{file_path:path}")
async def get_file_route(session_id: str, file_path: str) -> Response:
    try:
        data, mime = await logged_call(
            logger,
            "api",
            "read_file",
            session_id,
            lambda: get_file_op(session_id, file_path),
            path=file_path,
        )
    except SandboxOpError as error:
        raise_op(error)
    return Response(content=data, media_type=mime)


@fastapi_app.post("/v1/sessions/{session_id}/execute")
async def execute_route(session_id: str, body: ExecuteBody) -> dict[str, object]:
    try:
        return await logged_call(
            logger,
            "api",
            "execute",
            session_id,
            lambda: execute_op(session_id, body.code, body.timeoutMs),
            code_chars=len(body.code),
            timeout_ms=body.timeoutMs,
        )
    except SandboxOpError as error:
        raise_op(error)


@fastapi_app.delete("/v1/sessions/{session_id}")
async def delete_session_route(session_id: str) -> dict[str, bool]:
    try:
        return await logged_call(
            logger,
            "api",
            "delete_session",
            session_id,
            lambda: delete_session_op(session_id),
        )
    except SandboxOpError as error:
        raise_op(error)


app = BearerAuthASGI(fastapi_app, public_paths=("/health",))
