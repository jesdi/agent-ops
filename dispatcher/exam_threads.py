"""An attempt's chat: `attempts/<n>/threads.json` is {"turns": [Turn, ...]}, oldest first.

A turn is a tip, a message in a question's thread, or a message in the general chat. This
module reads and writes that file, and only that file, and builds the document
`pr_exam.py context` takes for a turn from it and from what `exams` hands it of the attempt.
It knows nothing of the exam's state: `exams` decides, under the exams lock, which attempt may
take which turn, and holds that lock around every call here that writes."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, Literal, TypedDict

from dispatcher.state import write_json_atomic

TIPS = 3   # tips a question gets per attempt
LOST = "console restarted"

Kind = Literal["tip", "question", "general"]
Status = Literal["running", "done", "failed"]


class Turn(TypedDict):
    id: str
    kind: Kind
    question: str | None   # None for the general chat, whose kind is "general"
    message: str           # the draft of a tip, else the operator's message
    status: Status
    text: str              # the reply
    error: str | None      # why a failed turn failed
    after_submit: bool     # it finished once the attempt was submitted: it marks nothing
    started: str


@dataclass(frozen=True)
class QuestionAsk:
    """A tip on `question`, or a message in its thread, on attempt `attempt` of the exam on `head_sha`."""
    attempt: int
    head_sha: str
    kind: Literal["tip", "question"]
    question: str
    message: str   # the draft of a tip, else the operator's message


@dataclass(frozen=True)
class GeneralAsk:
    """A message in the general chat of attempt `attempt` of the exam on `head_sha`."""
    attempt: int
    head_sha: str
    message: str
    kind: Literal["general"] = "general"
    question: ClassVar[None] = None   # the general chat is the thread of no question


Ask = QuestionAsk | GeneralAsk


@dataclass(frozen=True)
class Prompt:
    """What `pr_exam.py context` takes for one turn: the attempt's exam, the head's diff and
    PR body, and the turn document."""
    exam: Path
    diff: Path
    body: Path
    turn: dict


def turns(adir: Path) -> list[Turn]:
    try:
        return json.loads((adir / "threads.json").read_text())["turns"]
    except FileNotFoundError:
        return []


def _save(adir: Path, all_turns: list[Turn]) -> None:
    write_json_atomic(adir / "threads.json", {"turns": all_turns})


def _of(all_turns: list[Turn], question: str | None) -> list[Turn]:
    """The turns of `question`'s thread; the general chat's for None."""
    return [t for t in all_turns if t["question"] == question]


def _tips(mine: list[Turn], *statuses: Status) -> int:
    return sum(t["kind"] == "tip" and t["status"] in statuses for t in mine)


def tips_held(all_turns: list[Turn], question: str) -> int:
    """The tips that count against the cap: a running tip holds its slot, a failed one gives it back."""
    return _tips(_of(all_turns, question), "done", "running")


def running(all_turns: list[Turn], question: str | None) -> bool:
    return any(t["status"] == "running" for t in _of(all_turns, question))


def _assists(turn: Turn) -> bool:
    """A finished tip or question-thread reply while the attempt was open; the general chat never assists."""
    return turn["status"] == "done" and turn["question"] is not None and not turn["after_submit"]


def assisted(all_turns: list[Turn]) -> list[str]:
    return sorted({t["question"] for t in all_turns if _assists(t)})


def _shown(turn: Turn) -> dict:
    return {k: turn[k] for k in ("id", "kind", "message", "text", "status", "error")}


def question_view(question: str, all_turns: list[Turn], tips_open: bool) -> dict:
    """What the page shows of `question`'s chat: its finished tips, the tips it may still take
    (None when the attempt takes none), whether it is assisted, and its thread."""
    mine = _of(all_turns, question)
    return {"tips": _tips(mine, "done"), "tips_left": TIPS - tips_held(all_turns, question) if tips_open else None,
            "assisted": any(map(_assists, mine)), "thread": [_shown(t) for t in mine]}


def general_view(all_turns: list[Turn]) -> list[dict]:
    return [_shown(t) for t in _of(all_turns, None)]


def reserve(adir: Path, ask: Ask) -> str:
    """Append `ask` as a running turn; its id."""
    turn: Turn = {"id": uuid.uuid4().hex, "kind": ask.kind, "question": ask.question, "message": ask.message,
                  "status": "running", "text": "", "error": None, "after_submit": False,
                  "started": datetime.now(timezone.utc).isoformat()}
    _save(adir, [*turns(adir), turn])
    return turn["id"]


def end(adir: Path, turn_id: str, text: str, error: str | None, after_submit: bool) -> bool:
    """Store the reply of a running turn, "done", or "failed" with `error`; False, changing
    nothing, when the attempt has no such turn still running (startup failed it, or the attempt
    is gone)."""
    all_turns = turns(adir)
    turn = next((t for t in all_turns if t["id"] == turn_id and t["status"] == "running"), None)
    if turn is None:
        return False
    turn.update(status="failed" if error else "done", text=text, error=error, after_submit=after_submit)
    _save(adir, all_turns)
    return True


def fail_running(adir: Path) -> None:
    """Fail every turn still running: its console is gone, so it counts for nothing."""
    all_turns = turns(adir)   # [] when the attempt was deleted since it was listed
    lost = [t for t in all_turns if t["status"] == "running"]
    for t in lost:
        t.update(status="failed", error=LOST)
    if lost:
        _save(adir, all_turns)


def document(adir: Path, ask: Ask, answered: dict) -> dict:
    """The turn document `pr_exam.py context` takes for `ask` on the attempt at `adir`, with the
    finished turns of its thread. `answered` is the attempt's side of a question turn's
    question, as `exams` read it: its submitted answer, points and feedback ({} before submit)."""
    done = [t for t in _of(turns(adir), ask.question) if t["status"] == "done"]
    return _tip_document(ask, done) if ask.kind == "tip" else _chat_document(ask, done, answered)


def _tip_document(ask: QuestionAsk, done: list[Turn]) -> dict:
    tips = [t["text"] for t in done if t["kind"] == "tip"]
    return {"kind": "tip", "question": ask.question, "draft": ask.message, "tip": 1 + len(tips), "tips": tips}


def _chat_document(ask: Ask, done: list[Turn], answered: dict) -> dict:
    thread = [{"role": r, "text": t[k]} for t in done for r, k in (("user", "message"), ("assistant", "text"))]
    target = {} if ask.question is None else {"question": ask.question, **answered}
    return {"kind": ask.kind, "message": ask.message, "thread": thread, **target}
