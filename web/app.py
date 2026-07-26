"""FastAPI app factory: route wiring only. All I/O is behind `sources`."""
from __future__ import annotations

from fastapi import Depends, FastAPI

from dispatcher.config import Config
from web.auth import Operator, TailscaleAuthMiddleware, current_operator


def create_app(cfg: Config, sources) -> FastAPI:
    app = FastAPI(title="agent-ops web console")
    app.add_middleware(TailscaleAuthMiddleware)

    @app.get("/api/health")
    def health(op: Operator = Depends(current_operator)):
        return {"ok": True, "operator": op.login}

    return app
