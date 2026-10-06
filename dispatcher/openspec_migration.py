"""One-time deploy migration to the openspec pipeline:
`python -m dispatcher.openspec_migration [--check] <state_dir>`, run by hand
with the dispatcher stopped (README, "Deploying the openspec pipeline").

Every task file the old flow wrote in the spec stage or at the old spec
review gate is set to the state an operator's Resume of a crashed spec task
leaves behind: stage `spec`, park `unpark-requested`, `crashed_stage` `spec`.
The next pass (main._resume_one) ends the old session if one still lives and
starts a fresh spec session from the stage prompt; it never continues the old
transcript. While the task waits for that turn it is parked, so no pass reads
what the old session wrote to `.agent/stage.json`, and the restart rewrites
that file before it spawns.

What the operator told the old session is not lost: the messages of a
converted task are queued again, so main._spawn_stage puts them below the
fresh stage prompt, oldest first with actor and time, and stamps them
delivered once.

The files are raw JSON here: state.load rejects the old stage and request
kind. A file with the `asked` key was written by the new flow or by an earlier
run, and is left alone; that is what makes a second run change nothing.

Nothing the migration writes is an approval. Every converted task gets
`asked` true, so it waits at the plan review gate whatever its track: its
history is unknown, or a human was already part of it. The same holds for an
old task that failed in plan, implement or review (`do-not-resume`): `asked`
is all it gets. The stage it writes is `spec` only, the request it writes is
none, and a stage, park or request kind it does not know is never coerced: the
file is reported and the run refused.

All or nothing: an old task in plan, implement or review (the operator drains
those before the deploy), a file that cannot be read, or an unknown value
refuses the whole run before the first write. A box converted by half is
worse than one refused. `--check` is that check alone: no lock, no write.

main.main() calls `unmigrated` before every pass and refuses to run while a
task of the old flow is on disk: one pass of the new dispatcher would save
such a file as if the new flow had written it.

Delete after the deploy, all of it together:
- dispatcher/openspec_migration.py (this module);
- tests/test_openspec_migration.py, tests/test_openspec_migration_acceptance.py;
- the guard in dispatcher/main.py (`_refuse_old_flow` and its two calls) and
  its tests in tests/test_openspec_migration.py;
- the `blocking` parameter of dispatcher/convergence.pass_lock;
- the README section "Deploying the openspec pipeline";
- the restart-inputs list in step 1 of prompts/spec.md, with its tests in
  tests/test_prompts.py."""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from dispatcher.convergence import pass_lock
from dispatcher.messages import MESSAGES_DIR
from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_LOGIN, PARK_REVIEW,
                              PARK_WAKE, Stage, task_key)

OLD_GATE = "awaiting-spec-review"
RESTART_STAGES = ("spec", OLD_GATE)
DRAIN_STAGES = ("plan", "implement", "review")
OLD_REQUEST = "spec-approval"
STAGES = {s.value for s in Stage} | {OLD_GATE}
PARKS = ("", PARK_HUMAN, PARK_CI, PARK_WAKE, PARK_LOGIN, PARK_REVIEW)
REQUESTS = ("answers", "plan-approval", OLD_REQUEST)
# What would bounce, park or fail the fresh session on its first signal.
COUNTERS = ("spec_retries", "plan_retries", "review_rounds", "gate_rounds",
            "e2e_rounds", "ci_rounds")
RETIRED = ("ticket_cursor", "artifact")
# The actor of a message does not tell an operator from the dispatcher: a
# Telegram reply is queued as "dispatcher" too (main._wake's default). So
# every message is queued again except the two texts the dispatcher itself
# writes: the result of a CI run and the tmux hand-off.
DISPATCHER_NOTES = ("E2E run ", "Your session was moved from tmux")

CONVERT, DRAIN, NO_RESUME, UNTOUCHED = ("converted", "must-drain",
                                        "do-not-resume", "untouched")
UNREADABLE, UNKNOWN = "unreadable", "unknown-value"


class _Unknown(ValueError):
    """A task file holds a value this command does not know."""


@dataclass(frozen=True)
class _Task:
    path: Path
    doc: dict
    kind: str
    messages: Path
    queue: str | None   # the message file's new text; None = leave it


def _request_kind(doc: dict) -> str | None:
    request = doc.get("operator_request")
    return request.get("kind", "") if isinstance(request, dict) else request


def _check_values(doc: dict) -> None:
    """An unknown value is never read as a known one."""
    kind = _request_kind(doc)
    for name, value, known in (("stage", doc.get("stage"), STAGES),
                               ("park", doc.get("park", ""), PARKS),
                               ("operator_request.kind", kind, (None, *REQUESTS))):
        if value not in known:
            raise _Unknown(f"field {name} has the value {value!r}")
    if kind == OLD_REQUEST and doc["stage"] not in RESTART_STAGES:
        raise _Unknown(f"field operator_request.kind has the value {kind!r} "
                       f"on a task in stage {doc['stage']!r}")


def _read(path: Path) -> dict:
    """The raw task file. ValueError when it is not one, _Unknown when it
    holds a value the migration cannot judge."""
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    if (type(doc.get("issue")) is not int or not isinstance(doc.get("target"), str)
            or not isinstance(doc.get("stage"), str)):
        raise ValueError("no issue, target or stage")
    _check_values(doc)
    return doc


def _classify(doc: dict) -> str:
    stage = doc["stage"]
    if "asked" in doc and stage != OLD_GATE:
        return UNTOUCHED   # new flow, or handled by an earlier run
    crashed = doc.get("crashed_stage") if stage == "failed" else None
    if stage in RESTART_STAGES or crashed in RESTART_STAGES:
        return CONVERT     # failed: a Resume would start the old flow again
    if crashed in DRAIN_STAGES:
        return NO_RESUME   # a Resume would run the new flow on old artifacts
    return DRAIN if stage in DRAIN_STAGES else UNTOUCHED


def _convert(doc: dict) -> dict:
    """`spec_path` is cleared: the old design file must never reach the
    publish backstop (it commits the artifact's whole folder, docs/specs/),
    and the spec session finds the file on the branch by itself. A failed
    task stays failed; only what its Resume starts changes. `asked` is true
    for every task, with no attempt to read its history: a converted task
    never skips the plan review gate. `gated` is false: it never waited at
    that gate."""
    out = {k: v for k, v in doc.items() if k not in RETIRED}
    out.update(dict.fromkeys(COUNTERS, 0), asked=True, gated=False,
               spec_path="", operator_request=None, crashed_stage="spec")
    if doc["stage"] != "failed":
        out.update(stage="spec", park=PARK_WAKE)
    return out


def _queued_again(raw: str) -> str:
    """One line of a message file, with its delivery stamp removed unless
    the dispatcher wrote the message. A line that is no message is kept as
    it is: messages.py skips it too."""
    try:
        msg = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(msg, dict) or not msg.get("delivered_at") or not msg.get("text"):
        return raw
    if msg.get("actor") == "dispatcher" and str(msg["text"]).startswith(DISPATCHER_NOTES):
        return raw
    return json.dumps({**msg, "delivered_at": None})


def _queue(messages: Path) -> str | None:
    """The message file's text with the operator's messages queued again;
    None when there is nothing to write."""
    if not messages.exists():
        return None
    try:
        old = messages.read_text()
    except (OSError, ValueError) as exc:
        raise ValueError(f"{MESSAGES_DIR}/{messages.name}: {exc}") from exc
    new = "".join(_queued_again(raw) + "\n" for raw in old.splitlines())
    return new if new != old else None


def _task(state_dir: Path, path: Path) -> _Task:
    doc = _read(path)
    kind = _classify(doc)
    messages = (state_dir / MESSAGES_DIR
                / f"{task_key(doc['target'], doc['issue'])}.jsonl")
    return _Task(path, doc, kind, messages,
                 _queue(messages) if kind == CONVERT else None)


def _load(state_dir: Path) -> tuple[list[_Task], list[str]]:
    """The check phase: every task file with its outcome, and one line per
    file the migration cannot judge. Nothing is written here."""
    tasks, problems = [], []
    for path in sorted(state_dir.glob("task-*.json")):
        try:
            tasks.append(_task(state_dir, path))
        except _Unknown as exc:
            problems.append(f"{UNKNOWN}  {path.name}: {exc}")
        except (OSError, ValueError) as exc:
            problems.append(f"{UNREADABLE}  {path.name}: {exc}")
    return tasks, problems


def unmigrated(state_dir: str | Path) -> list[str]:
    """The task files a dispatcher pass must not see before the migration
    ran: whatever the migration would convert, mark or refuse to pass. One
    raw read per task file. A file that is not JSON is skipped, as the
    loader skips it: it is not a reason to stop the dispatcher."""
    names = []
    for path in sorted(Path(state_dir).glob("task-*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(doc, dict) and isinstance(doc.get("stage"), str)
                and _classify(doc) != UNTOUCHED):
            names.append(path.name)
    return names


def _replace_text(path: Path, text: str) -> None:
    """Temp file + replace, as the state module writes; no temp file stays
    behind when the write fails."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write(task: _Task) -> None:
    """The messages first: a run that stops between the two writes leaves
    the task file old, and the next run does the task again."""
    if task.kind == CONVERT:
        if task.queue is not None:
            _replace_text(task.messages, task.queue)
        _replace_text(task.path, json.dumps(_convert(task.doc), indent=2))
    elif task.kind == NO_RESUME:
        _replace_text(task.path, json.dumps({**task.doc, "asked": True}, indent=2))


def _line(label: str, doc: dict) -> str:
    stage = doc["stage"]
    if stage == "failed":
        stage += (f", crashed in {doc['crashed_stage']}" if doc.get("crashed_stage")
                  else ", not resumable")
    return f"{label}  {doc['target']} #{doc['issue']}  ({stage})"


def _refusal(drains: int, problems: list[str]) -> str:
    reasons = ([f"{drains} task(s) of the old flow in plan, implement or "
                f"review: the box was not drained"] if drains else [])
    if any(p.startswith(UNREADABLE) for p in problems):
        reasons.append("a file could not be read: repair it")
    if any(p.startswith(UNKNOWN) for p in problems):
        reasons.append("a task file holds a value this command does not know: "
                       "look at the named field; do not delete the file")
    return "Refused, nothing was changed. " + "; ".join(reasons) + "."


def _run(state_dir: Path, write: bool) -> int:
    tasks, problems = _load(state_dir)
    counts = Counter(t.kind for t in tasks)
    refused = bool(problems or counts[DRAIN])
    will = "converted" if write else "would-convert"
    for task in tasks:
        label = task.kind if task.kind != CONVERT else (
            "not converted" if refused else will)
        if write and not refused:
            try:
                _write(task)
            except OSError as exc:
                print(f"stopped at {task.path.name}: {exc}; run again")
                return 1
        print(_line(label, task.doc))
    for line in problems:
        print(line)
    print(_refusal(counts[DRAIN], problems) if refused else
          f"{counts[CONVERT]} {will}, {counts[UNTOUCHED]} untouched, "
          f"{counts[DRAIN]} must-drain, {counts[NO_RESUME]} do-not-resume."
          + ("" if write else " Check only: nothing was changed."))
    return int(refused)


def _locked_run(state_dir: Path) -> int:
    try:
        # The lock a dispatcher pass and the updater take. Never waited for:
        # a pass that holds it means the dispatcher was not stopped.
        with pass_lock(str(state_dir), blocking=False):
            return _run(state_dir, write=True)
    except BlockingIOError:
        print(f"{state_dir / 'convergence.lock'} is held: a dispatcher pass or "
              f"the updater is running. Stop them, then run this command "
              f"again. Nothing was changed.")
        return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    if check:
        args.remove("--check")
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: python -m dispatcher.openspec_migration [--check] <state_dir>")
        return 2
    state_dir = Path(args[0])
    if not state_dir.is_dir():
        print(f"state directory {state_dir} does not exist; nothing was changed")
        return 2
    return _run(state_dir, write=False) if check else _locked_run(state_dir)


if __name__ == "__main__":
    sys.exit(main())
