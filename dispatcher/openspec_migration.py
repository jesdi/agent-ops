"""One-time deploy migration to the openspec pipeline:
`python -m dispatcher.openspec_migration <state_dir>`, run by hand with the
dispatcher stopped (README, "Deployment").

Every task file the old flow wrote in the spec stage or at the old spec
review gate is set to the state an operator's Resume of a crashed spec task
leaves behind: stage `spec`, park `unpark-requested`, `crashed_stage` `spec`.
The next pass (main._resume_one) ends the old session if one still lives and
starts a fresh spec session from the stage prompt; it never continues the old
transcript. While the task waits for that turn it is parked, so no pass reads
what the old session wrote to `.agent/stage.json`, and the restart rewrites
that file before it spawns.

The files are raw JSON here: state.load rejects the old stage and request
kind. A file with the `asked` key was written by the new flow or by an earlier
run, and is left alone; that is what makes a second run change nothing.

Nothing the migration writes is an approval. Every converted task gets
`asked` true, so it waits at the plan review gate whatever its track: its
history is unknown, or a human was already part of it. The stage it writes is
`spec` only, the request it writes is none, and a stage, park or request kind
it does not know is never coerced: the file is reported and the run refused.

All or nothing: an old task in plan, implement or review (the operator drains
those before the deploy), or a task file that cannot be read, refuses the
whole run. A box converted by half is worse than one refused.

Delete after the deploy, together with tests/test_openspec_migration*.py, the
README step and the `blocking` parameter of convergence.pass_lock."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dispatcher.convergence import pass_lock
from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_LOGIN, PARK_REVIEW,
                              PARK_WAKE, Stage, write_json_atomic)

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

CONVERT, DRAIN, UNTOUCHED = "converted", "must-drain", "untouched"


def _request_kind(doc: dict) -> str | None:
    request = doc.get("operator_request")
    return request.get("kind", "") if isinstance(request, dict) else request


def _problem(doc: object) -> str:
    """Why this is not a task file the migration can judge; "" when it is.
    An unknown value is never read as a known one."""
    if not isinstance(doc, dict):
        return "not a JSON object"
    if type(doc.get("issue")) is not int or not isinstance(doc.get("target"), str):
        return "no issue or target"
    kind = _request_kind(doc)
    for name, value, known in (("stage", doc.get("stage"), STAGES),
                               ("park", doc.get("park", ""), PARKS),
                               ("request kind", kind, (None, *REQUESTS))):
        if value not in known:
            return f"unknown {name} {value!r}"
    if kind == OLD_REQUEST and doc["stage"] not in RESTART_STAGES:
        return f"old {OLD_REQUEST} request on a {doc['stage']} task"
    return ""


def _read(path: Path) -> dict:
    """The raw task file. ValueError when the migration cannot judge it."""
    doc = json.loads(path.read_text())
    problem = _problem(doc)
    if problem:
        raise ValueError(problem)
    return doc


def _classify(doc: dict) -> str:
    stage = doc["stage"]
    if "asked" in doc and stage != OLD_GATE:
        return UNTOUCHED   # new flow, or converted by an earlier run
    if stage in RESTART_STAGES:
        return CONVERT
    if stage == "failed" and doc.get("crashed_stage") in RESTART_STAGES:
        return CONVERT     # a Resume would start the old flow again
    return DRAIN if stage in DRAIN_STAGES else UNTOUCHED


def _convert(doc: dict) -> dict:
    """`spec_path` is cleared: the old design file must never reach the
    publish backstop (it commits the artifact's whole folder, docs/specs/),
    and the spec session finds the file on the branch by itself. A failed
    task stays failed; only what its Resume starts changes. `asked` is true
    for every task, with no attempt to read its history: a converted task
    never skips the plan review gate. `gated` is false: it never waited at
    that gate."""
    out = {k: v for k, v in doc.items() if k != "ticket_cursor"}
    out.update(dict.fromkeys(COUNTERS, 0), asked=True, gated=False,
               spec_path="", operator_request=None, crashed_stage="spec")
    if doc["stage"] != "failed":
        out.update(stage="spec", park=PARK_WAKE)
    return out


def _line(label: str, doc: dict) -> str:
    crashed = (f", crashed in {doc['crashed_stage']}"
               if doc["stage"] == "failed" and doc.get("crashed_stage") else "")
    return (f"{label}  {doc['target']} #{doc['issue']}  "
            f"({doc['stage']}{crashed})")


def _load(state_dir: Path) -> tuple[list[tuple[Path, dict, str]], list[str]]:
    """(path, raw task, outcome) per readable task file, and one line per
    file that is not readable."""
    tasks, unreadable = [], []
    for path in sorted(state_dir.glob("task-*.json")):
        try:
            doc = _read(path)
        except (OSError, ValueError) as exc:
            unreadable.append(f"unreadable  {path.name}: {exc}")
            continue
        tasks.append((path, doc, _classify(doc)))
    return tasks, unreadable


def _refusal(drains: int, unreadable: int) -> str:
    reasons = ([f"{drains} task(s) in plan, implement or review: the box was "
                f"not drained; let them finish"] if drains else [])
    reasons += ([f"{unreadable} unreadable task file(s): repair or remove "
                 f"them"] if unreadable else [])
    return f"Refused, nothing was changed. {'; '.join(reasons)}. Then run this command again."


def _migrate(state_dir: Path) -> int:
    tasks, unreadable = _load(state_dir)
    counts = {kind: sum(1 for _, _, k in tasks if k == kind)
              for kind in (CONVERT, UNTOUCHED, DRAIN)}
    refused = bool(unreadable or counts[DRAIN])
    for path, doc, kind in tasks:
        if kind == CONVERT and not refused:
            write_json_atomic(path, _convert(doc))
        print(_line("not converted" if kind == CONVERT and refused else kind, doc))
    print("\n".join(unreadable + [_refusal(counts[DRAIN], len(unreadable))])
          if refused else
          f"{counts[CONVERT]} converted, {counts[UNTOUCHED]} untouched, "
          f"{counts[DRAIN]} must-drain.")
    return 1 if refused else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m dispatcher.openspec_migration <state_dir>")
        return 2
    state_dir = Path(args[0])
    if not state_dir.is_dir():
        print(f"state directory {state_dir} does not exist; nothing was changed")
        return 2
    try:
        # The lock a dispatcher pass and the updater take. Never waited for:
        # a pass that holds it means the dispatcher was not stopped.
        with pass_lock(str(state_dir), blocking=False):
            return _migrate(state_dir)
    except BlockingIOError:
        print(f"{state_dir / 'convergence.lock'} is held: a dispatcher pass or "
              f"the updater is running. Stop the dispatcher, then run this "
              f"command again. Nothing was changed.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
