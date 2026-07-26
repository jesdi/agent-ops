"""Tailscale identity boundary. tailscale serve injects Whois headers on
every proxied request; a request without them bypassed the proxy and is
rejected. The login becomes the actor on every write."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

HEADER = "Tailscale-User-Login"


@dataclass(frozen=True)
class Operator:
    login: str


def current_operator(request: Request) -> Operator:
    login = request.headers.get(HEADER, "")
    if not login:
        raise HTTPException(status_code=401, detail=f"missing {HEADER}")
    return Operator(login=login)


class TailscaleAuthMiddleware:
    """ASGI-level guard so static files, SSE and WebSocket handshakes are
    covered too — a route dependency alone would miss mounted apps."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        if headers.get(HEADER.lower()):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            resp = JSONResponse({"detail": f"missing {HEADER}"},
                                status_code=401)
            await resp(scope, receive, send)
        else:
            await receive()  # consume websocket.connect
            await send({"type": "websocket.close", "code": 4401})
