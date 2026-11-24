"""Exam request and state read. The dispatcher does the GitHub work."""
from __future__ import annotations

from typing import Annotated, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from dispatcher import exams as state
from dispatcher.config import Config
from dispatcher.exam_lifecycle import ExamState, exam_url
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


class Exam(BaseModel):
    """What every exam response says of the exam."""
    repo: str
    pr: int
    state: ExamState
    head_sha: str | None
    debt: bool


class ExamAccepted(Exam):
    url: str
    detail: str
    pending: bool


class ExamQuestion(BaseModel):
    id: str
    type: Literal["choice", "text"]
    prompt: str
    options: list[str] | None = None


class ExamQuestions(BaseModel):
    """The served projection of an attempt: never a key entry or a rubric."""
    version: int
    repo: str
    pr: int
    head_sha: str
    questions: list[ExamQuestion]


class ExamQuestionResult(BaseModel):
    id: str
    concept: str
    points: int | float
    explanation: str | None = None   # only for a question under 1 point
    hunk: str | None = None


class ExamAttempt(BaseModel):
    attempt: int
    passed: bool
    score: int | float
    max: int | float
    questions: list[ExamQuestionResult]


class ExamKeptAttempt(BaseModel):
    """A failed attempt kept: its served questions and its result, never a key."""
    attempt: int
    questions: list[ExamQuestion]
    result: ExamAttempt


class ExamView(Exam):
    detail: str
    attempt: int
    exam: ExamQuestions | None
    results: list[ExamAttempt]
    attempts: list[ExamKeptAttempt] = []


class ExamEntry(Exam):
    title: str
    url: str


class ExamsView(BaseModel):
    exams: list[ExamEntry]


def _url(console: str, doc: dict) -> str:
    return exam_url(console, doc["repo"], doc["pr"])


def router(sources, cfg: Callable[[], Config]) -> APIRouter:
    routes = APIRouter()

    @routes.post("/api/exam", response_model=ExamAccepted)
    def request_exam(body: ExamRequest, op: Operator = Depends(current_operator)):
        console = cfg().console_url
        if not console:
            raise HTTPException(503, "console_url is not set in targets.yaml")
        doc = sources.request_exam(body.repo, body.pr)
        return ExamAccepted(**doc, url=_url(console, doc), pending=True)

    @routes.get("/api/exams", response_model=ExamsView)
    def listing(op: Operator = Depends(current_operator)):
        console = cfg().console_url
        return ExamsView(exams=[ExamEntry(**doc, url=_url(console, doc)) for doc in sources.exams()])

    @routes.get("/api/exam/host")
    def host(op: Operator = Depends(current_operator)):
        found = state.app(cfg().state_dir)
        if found is None:
            raise HTTPException(503, "the exam App is not known yet")
        return found

    def view(repo: str, pr: int) -> ExamView:
        """The exam as its page reads it; the questions only while they wait for answers."""
        doc = sources.exam_state(repo, pr)
        if doc is None:
            raise HTTPException(404, "exam not found")
        waiting = doc["state"] == ExamState.OPEN
        state_dir = cfg().state_dir
        return ExamView(**doc, exam=state.public(state_dir, repo, pr, doc["attempt"]) if waiting else None,
                        attempts=state.kept(state_dir, doc))

    @routes.get("/api/exam/{owner}/{name}/{pr}", response_model=ExamView, response_model_exclude_unset=True)
    def read_exam(owner: Annotated[str, Path(pattern=rf"^{Slug}$")],
                  name: Annotated[str, Path(pattern=rf"^{Slug}$")],
                  pr: Annotated[int, Path(ge=1)], op: Operator = Depends(current_operator)):
        return view(f"{owner}/{name}", pr)

    @routes.post("/api/exam/{owner}/{name}/{pr}/answers", status_code=202, response_model=ExamView,
                 response_model_exclude_unset=True)
    def answer(body: Answers, owner: Annotated[str, Path(pattern=rf"^{Slug}$")],
               name: Annotated[str, Path(pattern=rf"^{Slug}$")],
               pr: Annotated[int, Path(ge=1)], op: Operator = Depends(current_operator)):
        outcome = sources.submit_exam(f"{owner}/{name}", pr, body.attempt, body.head_sha, body.answers)
        if outcome == "conflict":
            raise HTTPException(409, "this attempt is not open for answers")
        if outcome == "incomplete":
            raise HTTPException(422, "every question needs an answer")
        return view(f"{owner}/{name}", pr)

    return routes
