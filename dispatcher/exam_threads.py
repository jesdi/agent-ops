"""An attempt's chat: `attempts/<n>/threads.json` is {"turns": [Turn, ...]}, oldest first.

A turn is a tip, a message in a question's thread, or a message in the general chat. This
module reads and writes the file and builds the document `pr_exam.py context` takes for a
turn. It knows nothing of the exam's state: `exams` decides, under the exams lock, which
attempt may take which turn, and holds that lock around every call here that writes."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

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
class Ask:
    """A turn as the operator asks for it, on attempt `attempt` of the exam on `head_sha`."""
    attempt: int
    head_sha: str
    kind: Kind
    question: str | None   # None exactly for a general turn
    message: str


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


def end(adir: Path, turn_id: str, text: str, error: str, after_submit: bool) -> bool:
    """Store the reply of a running turn, "done", or "failed" with `error`; False when the
    attempt has no such turn."""
    all_turns = turns(adir)
    turn = next((t for t in all_turns if t["id"] == turn_id), None)
    if turn is None:
        return False
    turn.update(status="failed" if error else "done", text=text, error=error or None, after_submit=after_submit)
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


def document(adir: Path, ask: Ask) -> dict:
    """The turn document `pr_exam.py context` takes for `ask` on the attempt at `adir`, with the
    finished turns of its thread."""
    done = [t for t in _of(turns(adir), ask.question) if t["status"] == "done"]
    return _tip_document(ask, done) if ask.kind == "tip" else _chat_document(adir, ask, done)


def _tip_document(ask: Ask, done: list[Turn]) -> dict:
    tips = [t["text"] for t in done if t["kind"] == "tip"]
    return {"kind": "tip", "question": ask.question, "draft": ask.message, "tip": 1 + len(tips), "tips": tips}


def _chat_document(adir: Path, ask: Ask, done: list[Turn]) -> dict:
    doc = {"kind": ask.kind, "message": ask.message,
           "thread": [{"role": r, "text": t[k]} for t in done for r, k in (("user", "message"), ("assistant", "text"))]}
    if ask.question is not None:
        doc.update(question=ask.question, **_answered(adir, ask.question))
    return doc


def _answered(adir: Path, question: str) -> dict:
    """The attempt's side of `question` once submitted (answers.json is written only then): the
    answer as text, and once graded its points and the grader's feedback; {} before submit."""
    answers = adir / "answers.json"
    if not answers.exists():
        return {}
    answer = json.loads(answers.read_text()).get(question)
    return {"submitted": _answer_text(adir, question, answer), **_scored(adir, question)}


def _answer_text(adir: Path, question: str, answer) -> str:
    """A text answer as written, a choice answer as its option, a bypassed question's missing
    answer said so."""
    if isinstance(answer, str):
        return answer
    if answer is None:
        return "(no answer: the question was bypassed)"
    options = _options(adir, question)
    return f"option {answer}: {options[answer]}" if 0 <= answer < len(options) else f"option {answer}"


def _options(adir: Path, question: str) -> list[str]:
    exam = json.loads((adir / "exam.json").read_text())
    return next(q.get("options") or [] for q in exam["questions"] if q["id"] == question)


def _scored(adir: Path, question: str) -> dict:
    result = adir / "result.json"
    got = next((r for r in json.loads(result.read_text())["questions"] if r["id"] == question), {}) \
        if result.exists() else {}
    return {k: got[k] for k in ("points", "feedback") if got.get(k) not in (None, "")}
