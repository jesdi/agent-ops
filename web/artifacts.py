"""Read-only artifact endpoints; stable URLs survive content updates and cleanup."""
from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.responses import RedirectResponse, Response

from dispatcher.task_artifacts import StoredArtifact
from web.auth import Operator, current_operator
from web.sources import ArtifactResource

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer"}
PREVIEW_TYPES = {"text/html", "text/plain", "image/svg+xml", "image/png",
                 "image/jpeg", "image/webp", "image/gif", "application/pdf"}
SANDBOX = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src data:; font-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")


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


def _view(resource: ArtifactResource, target: str, issue: int) -> ArtifactView:
    item = resource.item
    url = f"/api/task/{quote(target, safe='')}/{issue}/artifacts/{item.id}"
    return ArtifactView(
        id=item.id, name=item.name, path=item.path, media_type=item.media_type,
        updated_at=item.updated_at, stage=item.stage, status=resource.status,
        url=url if resource.status in {"published", "local"} else "",
        github_url=item.publication.url if item.publication else "")


def _preview(item: StoredArtifact, content: bytes) -> Response:
    # Opaque origin permits inline interactions without console credentials.
    headers = {**HEADERS, "Content-Security-Policy": SANDBOX}
    media = "text/plain" if item.media_type == "text/markdown" else item.media_type
    if media not in PREVIEW_TYPES:
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(item.path.split('/')[-1], safe='')
    return Response(content, media_type=media, headers=headers)


def router(sources, find_task) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/task/{target}/{issue}/artifacts", response_model=ArtifactsView)
    def artifacts(target: str, issue: int, op: Operator = Depends(current_operator)):
        find_task(target, issue)
        index = sources.artifacts(target, issue)
        items = [_view(resource, target, issue) for resource in index.items]
        return ArtifactsView(items=items, expires_at=index.expires_at, expired=index.expired)

    @routes.get("/api/task/{target}/{issue}/artifacts/{artifact_id}")
    def open_artifact(target: str, issue: int, artifact_id: str,
                      op: Operator = Depends(current_operator)):
        find_task(target, issue)
        index = sources.artifacts(target, issue)
        resource = next((r for r in index.items if r.item.id == artifact_id), None)
        if resource is None:
            raise HTTPException(404, "artifact not found")
        return _open_resource(sources, target, issue, resource)


    return routes


def _open_resource(sources, target: str, issue: int, resource: ArtifactResource) -> Response:
    item = resource.item
    if resource.status == "published":
        return RedirectResponse(item.publication.url, status_code=302, headers=HEADERS)
    if resource.status == "expired":
        raise HTTPException(410, "artifact expired")
    if resource.status == "unavailable":
        raise HTTPException(404, "artifact content unavailable")
    content = sources.artifact_content(target, issue, item.id)
    if content is None:
        raise HTTPException(410, "artifact content no longer available")
    return _preview(item, content)
