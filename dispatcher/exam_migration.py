"""The flat-layout migration (pr-exam-host-assist ticket 01).

Before `attempts/<n>/`, each of these files sat at the exam level and held the last attempt
that reached the step writing it. `exams._load` moves them on first read, under the exams
lock. Delete this module, its call in `exams._load` and the flat-layout tests in
tests/test_exam_attempt_directories.py once the deploy has run and every exam on the box has
been read, as ADR 0004 does for task files. `exams.read` takes the exams lock only for this
migration, so it can drop it then."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

FLAT_STEPS = {"exam.json": "opened", "public.json": "opened", "answers.json": "submitted",
              "grades.json": "graded", "result.json": "graded"}
UNATTRIBUTED = "unattributed"   # where a flat file whose attempt the times cannot tell is kept


def flat_owner(doc: dict, name: str, after_submit: bool = False) -> int:
    """The attempt the flat file `name` belongs to: the current one once it reached the step
    that writes the file, else the one before, which did. Attempt 1 has none before it, so a
    file it wrote in a step that then failed (grades or result of an errored grading) is its.
    `after_submit`: the file was written after the current attempt's answers, so a grading of
    it that stopped before its summary wrote it, not the attempt before."""
    n = doc["attempt"]
    graded = any(r["attempt"] == n for r in doc["results"])
    grading = bool(doc["grading"])
    reached = {"opened": True, "submitted": graded or grading, "graded": graded or grading and after_submit}
    return n if reached[FLAT_STEPS[name]] else max(n - 1, 1)


def _written_after(path: Path, answers: Path) -> bool | None:
    """`path` was written after `answers`; None when the times cannot tell (no answers, or
    the same time, as a restore that lost their precision leaves). Renames keep both times."""
    if not answers.exists() or path.stat().st_mtime_ns == answers.stat().st_mtime_ns:
        return None
    return path.stat().st_mtime_ns > answers.stat().st_mtime_ns


def _owner(doc: dict, name: str, flat: Path, answers: Path) -> int | None:
    """`flat_owner` of the flat file, or None when it hangs on an order the times cannot tell."""
    after = _written_after(flat, answers)
    owners = {flat_owner(doc, name, a) for a in ((True, False) if after is None else (after,))}
    return owners.pop() if len(owners) == 1 else None


def backfill(doc: dict, n: int, result: Path, line: Callable[[object], str]) -> bool:
    """Give attempt `n`'s result summary the feedback its result.json holds, each through
    `line` as grading would have let it through; False when `n` has no summary."""
    summary = _summary(doc, n)
    if summary is None:
        return False
    lines = _feedback_lines(result, line)
    for q in summary["questions"]:
        if q["points"] < 1 and lines.get(q["id"]):
            q["feedback"] = lines[q["id"]]
    return True


def _summary(doc: dict, n: int) -> dict | None:
    """Attempt `n`'s result summary in state.json; None when it has none on this head."""
    return next((r for r in doc["results"] if r["attempt"] == n), None)


def _feedback_lines(result: Path, line: Callable[[object], str]) -> dict:
    """{question id: its validated feedback line} of a result.json."""
    return {q["id"]: line(q.get("feedback")) for q in json.loads(result.read_text())["questions"]}


def _target(d: Path, n: int | None) -> Path:
    """Attempt `n`'s directory; an ambiguous file (`n` None) is kept aside, in no attempt,
    rather than credited to the wrong one."""
    return d / (UNATTRIBUTED if n is None else f"attempts/{n}")


def migrate(d: Path, doc: dict, save: Callable[[], None], line: Callable[[object], str]) -> None:
    """Move each file the exam at `d` left in the flat layout into the directory of the attempt
    it belongs to. The caller holds the exams lock; `save` writes `doc`. Each file moves by an
    atomic rename, after its backfill is saved, and a file left at the flat path asks for the
    move again, so an interrupted migration resumes."""
    for name in FLAT_STEPS:
        flat = d / name
        if not flat.exists():
            continue
        answers = d / "attempts" / str(doc["attempt"]) / "answers.json"   # answers.json moved first
        n = _owner(doc, name, flat, answers)
        if n is not None and name == "result.json" and backfill(doc, n, flat, line):
            save()
        target = _target(d, n)
        target.mkdir(parents=True, exist_ok=True)
        flat.replace(target / name)
