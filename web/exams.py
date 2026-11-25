"""Exam request and state read. The dispatcher does the GitHub work."""
from __future__ import annotations

from typing import Annotated, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from dispatcher import exams as state
from dispatcher.config import Config
from dispatcher.exam_lifecycle import ExamState, exam_url
from web.auth import Operator, current_operator

Slug = r"[A-Za-z0-9][A-Za-z0-9._-]*"


Owner = Annotated[str, Path(pattern=rf"^{Slug}$")]
Pr = Annotated[int, Path(ge=1)]


class ExamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(pattern=rf"^{Slug}/{Slug}$")
    pr: int = Field(strict=True, ge=1)


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: int = Field(strict=True)
    head_sha: str
    answers: dict[str, Annotated[int, Field(strict=True)] | str]


class Bypass(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: int = Field(strict=True)
    head_sha: str
    question: str


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


class ExamTurn(BaseModel):
    id: str
    kind: Literal["tip", "question", "general"]
    message: str
    text: str
    status: Literal["running", "done", "failed"]
    error: str | None


class ExamQuestion(BaseModel):
    id: str
    type: Literal["choice", "text"]
    prompt: str
    options: list[str] | None = None
    tips: int   # the question's finished tips in this attempt
    tips_left: int | None   # the tips it may still take; None when the attempt takes none
    assisted: bool
    thread: list[ExamTurn]


class AttemptChat(BaseModel):
    """An attempt's served questions with their chat, never a key entry or a rubric, and what
    it takes now: `chat` for question and general turns."""
    questions: list[ExamQuestion]
    general: list[ExamTurn]   # the attempt's general chat
    chat: bool


class ExamQuestions(AttemptChat):
    """The open attempt, as the skill's projection serves it; `bypass` while it takes one."""
    version: int
    repo: str
    pr: int
    head_sha: str
    bypass: bool


class ExamQuestionResult(BaseModel):
    id: str
    concept: str
    points: int | float | None = None   # absent for a bypassed question
    help: Literal["bypassed", "assisted"] | None = None
    explanation: str | None = None   # only for a question under 1 point
    hunk: str | None = None
    feedback: str | None = None   # the grader's line, only for a missed or partial free-text question


class ExamResult(BaseModel):
    passed: bool
    score: int | float
    max: int | float
    questions: list[ExamQuestionResult]


class ExamAttempt(AttemptChat):
    """A submitted attempt of this head, and its result once graded."""
    attempt: int
    result: ExamResult | None


class ExamView(Exam):
    detail: str
    attempt: int
    tip_cap: int   # the tips a question gets per attempt
    exam: ExamQuestions | None   # the open attempt, while it waits for answers
    bypassed: str | None   # the open attempt's bypassed question
    attempts: list[ExamAttempt]


class ExamEntry(Exam):
    title: str
    url: str


class ExamsView(BaseModel):
    exams: list[ExamEntry]


STATUS = {state.Refusal.CONFLICT: 409, state.Refusal.BUSY: 409, state.Refusal.UNKNOWN: 422,
          state.Refusal.INCOMPLETE: 422, state.Refusal.TOO_LONG: 422, state.Refusal.USAGE: 429,
          state.Refusal.UNAVAILABLE: 503}


def refused(request: Request, exc: state.Refused) -> JSONResponse:
    """The one HTTP answer to a change the exam refused."""
    return JSONResponse({"detail": exc.detail}, status_code=STATUS[exc.refusal])


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
        doc = sources.exam_view(repo, pr)
        if doc is None:
            raise HTTPException(404, "exam not found")
        return ExamView(**doc)

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
        sources.submit_exam(f"{owner}/{name}", pr, body.attempt, body.head_sha, body.answers)
        return view(f"{owner}/{name}", pr)

    def set_bypass(body: Bypass, owner: str, name: str, pr: int, on: bool) -> ExamView:
        sources.bypass_exam(f"{owner}/{name}", pr, body.attempt, body.head_sha, body.question, on)
        return view(f"{owner}/{name}", pr)

    @routes.post("/api/exam/{owner}/{name}/{pr}/bypass", response_model=ExamView, response_model_exclude_unset=True)
    def bypass(body: Bypass, owner: Owner, name: Owner, pr: Pr, op: Operator = Depends(current_operator)):
        return set_bypass(body, owner, name, pr, True)

    @routes.delete("/api/exam/{owner}/{name}/{pr}/bypass", response_model=ExamView, response_model_exclude_unset=True)
    def unbypass(body: Bypass, owner: Owner, name: Owner, pr: Pr, op: Operator = Depends(current_operator)):
        return set_bypass(body, owner, name, pr, False)

    return routes
