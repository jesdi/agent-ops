"""Read-only artifact endpoints; stable URLs survive content updates and cleanup."""
from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.responses import RedirectResponse, Response

from dispatcher import task_artifacts
from web.auth import Operator, current_operator


class ArtifactView(BaseModel):
    id: str
    name: str
    path: str
    media_type: str
    updated_at: str
    stage: str
    status: Literal["published", "local", "expired", "unavailable"]
    url: str
    github_url: str


class ArtifactsView(BaseModel):
    items: list[ArtifactView]
    expires_at: str
    expired: bool


def router(state_dir, find_task) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/task/{target}/{issue}/artifacts", response_model=ArtifactsView)
    def artifacts(target: str, issue: int, op: Operator = Depends(current_operator)):
        find_task(target, issue)
        index = task_artifacts.read(state_dir, target, issue)
        items = []
        for item in index["items"]:
            github = item.get("github_url", "")
            available = task_artifacts.content_path(state_dir, target, issue, item["id"])
            status = "published" if github else "expired" if index["expired"] else "local" if available else "unavailable"
            url = f"/api/task/{quote(target, safe='')}/{issue}/artifacts/{item['id']}"
            items.append(ArtifactView(**{k: item[k] for k in
                ("id", "name", "path", "media_type", "updated_at", "stage")},
                status=status, url=url if github or available else "", github_url=github))
        return ArtifactsView(items=items, expires_at=index["expires_at"], expired=index["expired"])

    @routes.get("/api/task/{target}/{issue}/artifacts/{artifact_id}")
    def open_artifact(target: str, issue: int, artifact_id: str,
                      op: Operator = Depends(current_operator)):
        find_task(target, issue)
        index = task_artifacts.read(state_dir, target, issue)
        item = next((i for i in index["items"] if i["id"] == artifact_id), None)
        if item is None:
            raise HTTPException(404, "artifact not found")
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer"}
        if item.get("github_url"):
            return RedirectResponse(item["github_url"], status_code=302, headers=headers)
        path = task_artifacts.content_path(state_dir, target, issue, artifact_id)
        if path is None:
            raise HTTPException(410 if index["expired"] else 404,
                                "artifact expired" if index["expired"] else "artifact content unavailable")
        # Opaque origin: generated HTML/SVG can run inline interactions but
        # cannot access the console, its credentials, APIs, or external assets.
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src data:; font-src data:; "
            "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
        media = item["media_type"]
        if media == "text/markdown":
            media = "text/plain"  # readable local fallback when GitHub publication failed
        if media not in {"text/html", "text/plain", "image/svg+xml", "image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}:
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(item["path"].split('/')[-1], safe='')
        try:
            return Response(path.read_bytes(), media_type=media, headers=headers)
        except FileNotFoundError:
            raise HTTPException(410, "artifact content no longer available")

    return routes
