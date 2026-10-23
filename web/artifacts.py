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
from web.task_ref import REF, TaskRef, task_routes

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
    repo: str = ""            # the repository holding it, for a multi-repository task
    task_revision: int = 0    # the task scope revision it was collected under
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
    spec_repo_choices: list[str] = []   # non-empty: publication waits for one of these


class SpecRepoReq(BaseModel):
    spec_repo: str


def _view(resource: ArtifactResource, target: str, issue: int, repo: str) -> ArtifactView:
    item = resource.item
    where = f"{quote(target, safe='')}/{repo.replace('/', '~')}/{issue}" if repo \
        else f"{quote(target, safe='')}/{issue}"
    url = f"/api/task/{where}/artifacts/{item.id}"
    return ArtifactView(
        id=item.id, name=item.name, path=item.path, repo=item.repo,
        task_revision=item.task_revision, media_type=item.media_type,
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

    @task_routes(routes, "get", "/artifacts", response_model=ArtifactsView)
    def artifacts(ref: TaskRef = REF, op: Operator = Depends(current_operator)):
        task = find_task(ref)
        index = sources.artifacts(task.target, task.issue, task.repo)
        items = [_view(resource, task.target, task.issue, task.repo)
                 for resource in index.items]
        return ArtifactsView(items=items, expires_at=index.expires_at, expired=index.expired,
                             spec_repo_choices=list(index.spec_repo_choices))

    @task_routes(routes, "post", "/spec-repo", status_code=202)
    def choose_spec_repo(req: SpecRepoReq, ref: TaskRef = REF,
                         op: Operator = Depends(current_operator)):
        task = find_task(ref)
        index = sources.artifacts(task.target, task.issue, task.repo)
        if req.spec_repo not in index.spec_repo_choices:
            raise HTTPException(422, "not a repository the spec/plan is waiting on")
        name = sources.submit_intent("spec-repo", task.target, task.issue,
                                     {"spec_repo": req.spec_repo}, op.login, task.repo)
        return {"status": "pending", "intent": name}

    @task_routes(routes, "get", "/artifacts/{artifact_id}")
    def open_artifact(artifact_id: str, ref: TaskRef = REF,
                      op: Operator = Depends(current_operator)):
        task = find_task(ref)
        index = sources.artifacts(task.target, task.issue, task.repo)
        resource = next((r for r in index.items if r.item.id == artifact_id), None)
        if resource is None:
            raise HTTPException(404, "artifact not found")
        return _open_resource(sources, task, resource)


    return routes


def _open_resource(sources, task, resource: ArtifactResource) -> Response:
    item = resource.item
    if resource.status == "published":
        return RedirectResponse(item.publication.url, status_code=302, headers=HEADERS)
    if resource.status == "expired":
        raise HTTPException(410, "artifact expired")
    if resource.status == "unavailable":
        raise HTTPException(404, "artifact content unavailable")
    content = sources.artifact_content(task.target, task.issue, item.id, task.repo)
    if content is None:
        raise HTTPException(410, "artifact content no longer available")
    return _preview(item, content)
