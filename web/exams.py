"""Exam request and state read. The dispatcher does the GitHub work."""
from __future__ import annotations

from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from dispatcher import exams as state
from dispatcher.config import Config
from web.auth import Operator, current_operator

Slug = r"[A-Za-z0-9][A-Za-z0-9._-]*"


class ExamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=rf"^{Slug}/{Slug}$")
    pr: int = Field(strict=True, ge=1)


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: int = Field(strict=True)
    head_sha: str
    answers: dict[str, Annotated[int, Field(strict=True)] | str]


class ExamAccepted(BaseModel):
    repo: str
    pr: int
    url: str
    state: str
    head_sha: str | None
    debt: bool
    detail: str
    pending: bool


class ExamView(BaseModel):
    repo: str
    pr: int
    state: str
    head_sha: str | None
    debt: bool
    detail: str
    attempt: int
    exam: dict | None
    results: list[dict]


def router(sources, cfg: Callable[[], Config]) -> APIRouter:
    routes = APIRouter(prefix="/api/exam")

    @routes.post("", response_model=ExamAccepted)
    def request_exam(body: ExamRequest, op: Operator = Depends(current_operator)):
        console = cfg().console_url
        if not console:
            raise HTTPException(503, "console_url is not set in targets.yaml")
        doc = sources.request_exam(body.repo, body.pr)
        return ExamAccepted(**{k: doc[k] for k in ("repo", "pr", "state", "head_sha", "debt", "detail")},
                            url=f"{console}/exam/{body.repo}/{body.pr}", pending=True)

    @routes.get("/host")
    def host(op: Operator = Depends(current_operator)):
        found = state.app(cfg().state_dir)
        if found is None:
            raise HTTPException(503, "the exam App is not known yet")
        return found

    @routes.get("/{owner}/{name}/{pr}", response_model=ExamView)
    def read_exam(owner: Annotated[str, Path(pattern=rf"^{Slug}$")],
                  name: Annotated[str, Path(pattern=rf"^{Slug}$")],
                  pr: Annotated[int, Path(ge=1)], op: Operator = Depends(current_operator)):
        doc = sources.exam_state(f"{owner}/{name}", pr)
        if doc is None:
            raise HTTPException(404, "exam not found")
        return ExamView(**{k: doc[k] for k in ("repo", "pr", "state", "head_sha", "debt", "detail", "attempt")},
                        exam=state.public(cfg().state_dir, doc["repo"], doc["pr"]) if doc["state"] == "open" else None,
                        results=doc.get("results", []))

    @routes.post("/{owner}/{name}/{pr}/answers")
    def answer(body: Answers, owner: Annotated[str, Path(pattern=rf"^{Slug}$")],
               name: Annotated[str, Path(pattern=rf"^{Slug}$")],
               pr: Annotated[int, Path(ge=1)], op: Operator = Depends(current_operator)):
        outcome = sources.submit_exam(f"{owner}/{name}", pr, body.attempt, body.head_sha, body.answers)
        if outcome == "conflict":
            raise HTTPException(409, "this attempt is not open for answers")
        if outcome == "incomplete":
            raise HTTPException(422, "every question needs an answer")
        return {"state": "grading"}

    return routes
