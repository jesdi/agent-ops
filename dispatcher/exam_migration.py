"""The migration of an exam from before pr-exam-host-assist (ticket 01's flat layout).

Before `attempts/<n>/`, each per-attempt file sat at the exam level and held the last attempt
that reached the step writing it. `exams._load` calls `migrate` on every read, under the exams
lock: it moves each flat file into its attempt, then, once per exam (a state.json with no
`bypassed` key is one from before), fills in what the assist layout always has, so the rest of
the code reads one shape:
- `bypassed` in state.json;
- `flags.json` (nothing bypassed or assisted) for every submitted attempt;
- the exam's `diff.patch` and `body.md` the chat is given: the passed diff, else the last
  job's inputs, else a line saying it was not kept;
- `public.json` for a graded attempt whose questions the flat layout replaced, built from its
  result summary, each prompt saying the question was not kept. Such an attempt has no
  exam.json, so it takes no turn: `lost`, which `exams._chatting` calls.

Once the deploy has run and every exam on the box has been read, delete this module, its call
in `exams._load` and the flat-layout tests in tests/test_exam_attempt_directories.py, as
ADR 0004 does for task files: nothing else reads the old shape. `exams.read` takes the exams
lock only for this migration, so it can drop it then."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from dispatcher.state import write_atomic, write_json_atomic

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
    """Bring the exam at `d` from before the assist layout to it. The caller holds the exams
    lock; `save` writes `doc`. Every step is idempotent and `bypassed` is saved last, so an
    interrupted migration resumes."""
    _move_flat(d, doc, save, line)
    if "bypassed" in doc:
        return
    _fill(d, doc)
    doc["bypassed"] = None
    save()


def _move_flat(d: Path, doc: dict, save: Callable[[], None], line: Callable[[object], str]) -> None:
    """Move each file the exam at `d` left in the flat layout into the directory of the attempt
    it belongs to. Each file moves by an atomic rename, after its backfill is saved, and a file
    left at the flat path asks for the move again."""
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


NOT_KEPT = {"diff.patch": "(the diff of this exam was not kept)", "body.md": "(the PR body of this exam was not kept)"}


def _fill(d: Path, doc: dict) -> None:
    """What the assist layout always has and an exam from before it may lack."""
    if not doc["attempt"]:
        return
    _fill_chat_inputs(d)
    for n in [r["attempt"] for r in doc["results"]] + ([doc["grading"]["attempt"]] if doc["grading"] else []):
        _fill_attempt(d / "attempts" / str(n), doc, n)


def _fill_chat_inputs(d: Path) -> None:
    """The exam's chat diff and body from what the exam kept: the passed diff, else the last
    job's inputs (`job/` stays after a job), else a line saying it was not kept. A symlink in
    `job/`, which the session could write, is never followed."""
    kept = {"diff.patch": (d / "passed.diff", d / "job" / "diff.patch"), "body.md": (d / "job" / "body.md",)}
    for name, found in kept.items():
        if not (d / name).exists():
            write_atomic(d / name, next((p.read_text() for p in found if p.is_file() and not p.is_symlink()), NOT_KEPT[name]))


def _fill_attempt(adir: Path, doc: dict, n: int) -> None:
    adir.mkdir(parents=True, exist_ok=True)
    if (adir / "answers.json").exists() and not (adir / "flags.json").exists():
        write_json_atomic(adir / "flags.json", {"bypassed": [], "assisted": []})
    summary = _summary(doc, n)
    if summary and not (adir / "public.json").exists():
        write_json_atomic(adir / "public.json", {
            "version": 1, "repo": doc["repo"], "pr": doc["pr"], "head_sha": doc["head_sha"],
            "questions": [{"id": q["id"], "type": "text", "prompt": f"{q['concept']}: this question was not kept."}
                          for q in summary["questions"]]})


def lost(adir: Path) -> bool:
    """Attempt `adir` lost its exam to the flat layout (`_fill_attempt` kept only its result), so
    no turn can be built from it."""
    return not (adir / "exam.json").is_file()
