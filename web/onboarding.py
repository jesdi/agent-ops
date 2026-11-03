"""Authenticated onboarding views and intent submission; the unit owns progress."""
from __future__ import annotations

import re
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field, field_validator, model_validator

from dispatcher import config, onboard
from web.auth import Operator, current_operator


ProjectName = Annotated[str, Path(pattern=rf"^{config._SLUG.pattern}$")]


class OnboardingBoard(BaseModel):
    owner: str
    number: int = Field(strict=True)


class OnboardingCommands(BaseModel):
    setup_cmd: str = ""
    gate_cmd: str
    verify_cmd: str = ""


class OnboardingRequest(BaseModel):
    repos: list[str]
    board: OnboardingBoard
    commands: list[OnboardingCommands]


class OnboardingStartRequest(OnboardingRequest):
    expected_request_revision: str | None = None
    repos: list[str] = Field(min_length=1)

    @field_validator("repos")
    @classmethod
    def github_repositories(cls, values: list[str]) -> list[str]:
        if any(onboard.slug(value) is None for value in values):
            raise ValueError("repositories must be GitHub owner/name or URLs")
        keys = [onboard.repo_key(value) for value in values]
        if len(set(keys)) != len(keys):
            raise ValueError("each repository must appear once")
        return values

    @field_validator("board")
    @classmethod
    def existing_board(cls, value: OnboardingBoard) -> OnboardingBoard:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", value.owner) or value.number < 1:
            raise ValueError("board must name an owner and a positive number")
        return value

    @field_validator("commands")
    @classmethod
    def required_gates(cls, values: list[OnboardingCommands]) -> list[OnboardingCommands]:
        if any(not value.gate_cmd.strip() for value in values):
            raise ValueError("gate command is required for each repository")
        return values

    @model_validator(mode="after")
    def commands_match(self):
        if len(self.commands) != len(self.repos):
            raise ValueError("one commands entry is required per repository")
        return self


class OnboardingRepository(OnboardingCommands):
    name: str
    repo: str
    github_id: int
    clone_path: str
    worktrees_path: str


class OnboardingProposal(BaseModel):
    name: str
    paused: bool
    board: OnboardingBoard
    repositories: list[OnboardingRepository]


class OnboardingPending(BaseModel):
    id: str
    action: Literal["add", "confirm", "resume"]
    actor: str
    created_at: str


class OnboardingStep(BaseModel):
    name: str
    state: onboard.StepState
    detail: str
    link: str
    since: str


class OnboardingView(BaseModel):
    project: str
    status: Literal["queued", "open", "complete"]
    request: OnboardingRequest
    request_revision: str
    edit_mode: onboard.EditMode
    proposal: OnboardingProposal | None
    revision: str
    confirmed_revision: str
    awaiting: str
    notice: str
    steps: list[OnboardingStep]
    pr_url: str
    updated_at: str
    pending: list[OnboardingPending]


class OnboardingsView(BaseModel):
    onboardings: list[OnboardingView]


class OnboardingConfirm(BaseModel):
    revision: str = Field(min_length=1)


class OnboardingAccepted(BaseModel):
    request_revision: str | None = None
    status: Literal["pending"] = "pending"
    intent: str


def _view(project: str, record, pending: list[dict]) -> OnboardingView:
    if record is not None:
        return OnboardingView(**{**asdict(record), "proposal": record.proposal or None,
                                 "pr_url": record.pr_url, "pending": pending,
                                 "request_revision": onboard.request_revision(record.request),
                                 "edit_mode": onboard.edit_mode(record)})
    request = next((p["payload"] for p in pending if p["action"] == "add"), None)
    if request is None:
        raise HTTPException(404, "onboarding not found")
    return OnboardingView(project=project, status="queued", request=request,
                          request_revision=onboard.request_revision(request), edit_mode="none",
                          proposal=None, revision="", confirmed_revision="", awaiting="",
                          notice="", steps=[], pr_url="", updated_at="", pending=pending)


def router(sources) -> APIRouter:
    routes = APIRouter(prefix="/api/onboarding")

    @routes.get("", response_model=OnboardingsView)
    def list_onboardings(op: Operator = Depends(current_operator)):
        pending = sources.onboarding_pending()
        records = {r.project: r for r in sources.onboarding_records()}
        projects = sorted(records.keys() | {p["project"] for p in pending if p["action"] == "add"})
        return OnboardingsView(onboardings=[_view(
            project, records.get(project), [p for p in pending if p["project"] == project])
            for project in projects])

    @routes.get("/{project}", response_model=OnboardingView)
    def detail(project: ProjectName, op: Operator = Depends(current_operator)):
        pending = [p for p in sources.onboarding_pending() if p["project"] == project]
        return _view(project, sources.onboarding_record(project), pending)

    @routes.post("/{project}", status_code=202, response_model=OnboardingAccepted)
    def start(project: ProjectName, request: OnboardingStartRequest, op: Operator = Depends(current_operator)):
        payload = request.model_dump(exclude={"expected_request_revision"})
        expected = request.expected_request_revision or ""
        conflict = onboard.request_conflict(sources.onboarding_record(project), payload, expected)
        if conflict:
            raise HTTPException(409, conflict)
        return OnboardingAccepted(
            request_revision=onboard.request_revision(payload),
            intent=sources.submit_onboarding(
                "add", project, {**payload, "expected_request_revision": expected}, op.login))

    @routes.post("/{project}/confirm", status_code=202, response_model=OnboardingAccepted)
    def confirm(project: ProjectName, request: OnboardingConfirm,
                op: Operator = Depends(current_operator)):
        record = _open_record(sources, project)
        if record.awaiting != "confirm" or record.revision != request.revision:
            raise HTTPException(409, "proposal changed; review it and confirm again")
        return OnboardingAccepted(intent=sources.submit_onboarding(
            "confirm", project, request.model_dump(), op.login))

    @routes.post("/{project}/resume", status_code=202, response_model=OnboardingAccepted)
    def resume(project: ProjectName, op: Operator = Depends(current_operator)):
        _open_record(sources, project)
        return OnboardingAccepted(intent=sources.submit_onboarding("resume", project, {}, op.login))

    return routes


def _open_record(sources, project: str) -> onboard.Record:
    record = sources.onboarding_record(project)
    if record is None:
        raise HTTPException(404, "onboarding not found")
    if record.status != "open":
        raise HTTPException(409, "onboarding is already complete")
    return record
