from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Awaitable, Callable, Collection
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

UNAUTHORIZED_BODY = b'{"detail":"unauthorized"}'


def bearer_authorized(authorization: str | None) -> bool:
    expected = os.environ.get("SANDBOX_SECRET", "")
    if len(expected) == 0:
        return False
    provided = ""
    if authorization is not None and authorization.startswith("Bearer "):
        provided = authorization[7:]
    digest = hashlib.sha256
    return hmac.compare_digest(digest(provided.encode("utf-8")).digest(), digest(expected.encode("utf-8")).digest())


class BearerAuthASGI:
    def __init__(self, app: ASGIApp, *, public_paths: Collection[str] = ()) -> None:
        self.app = app
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self.public_paths:
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        if not bearer_authorized(headers.get("authorization")):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})
            return
        await self.app(scope, receive, send)
