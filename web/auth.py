"""Tailscale identity boundary. tailscale serve injects Whois headers on
every proxied request; a request without them bypassed the proxy and is
rejected. The login becomes the actor on every write."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

HEADER = "Tailscale-User-Login"
HEADER_KEY = HEADER.lower().encode()
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Operator:
    login: str


def _raw_header(scope, key: bytes) -> bytes | None:
    """Look a header up in the raw ASGI list, without decoding every header
    on every request (non-UTF-8 bytes must not raise)."""
    for k, v in scope.get("headers", []):
        if k.lower() == key:
            return v
    return None


def _host_of(origin: bytes) -> str:
    """Reduce an Origin (scheme://host[:port]) to a comparable host[:port]."""
    text = origin.decode("latin-1").strip().lower()
    _, _, rest = text.rpartition("://")
    return (rest or text).split("/", 1)[0]


def same_origin(scope) -> bool:
    """True when the request carries no Origin (curl, server-side clients) or
    an Origin whose host matches the Host the request was addressed to.

    tailscale serve injects the Whois header on every proxied request,
    including ones a third-party page triggers from the operator's browser,
    so the identity header alone does not establish first-party intent.
    """
    origin = _raw_header(scope, b"origin")
    if origin is None or not origin.strip():
        return True
    host = _raw_header(scope, b"host") or b""
    return _host_of(origin) == host.decode("latin-1").strip().lower()


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
        login = _raw_header(scope, HEADER_KEY)
        if not login or not login.strip():
            await self._reject(scope, receive, send, 401, 4401,
                               f"missing {HEADER}")
            return
        # Unsafe methods and WebSocket handshakes must also be first-party:
        # neither is protected by CORS preflight (body-less POSTs are simple
        # requests; WebSocket is not subject to CORS at all).
        unsafe = (scope["type"] == "websocket"
                  or scope.get("method", "").upper() not in SAFE_METHODS)
        if unsafe and not same_origin(scope):
            await self._reject(scope, receive, send, 403, 4403,
                               "cross-origin request refused")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send, status: int, ws_code: int,
                      detail: str) -> None:
        if scope["type"] == "http":
            await JSONResponse({"detail": detail},
                               status_code=status)(scope, receive, send)
        else:
            await receive()  # consume websocket.connect
            await send({"type": "websocket.close", "code": ws_code})
