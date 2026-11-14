"""A task file written before the status was stored (#132): the table of
the scenario "migration command rewrites each legacy shape", which reads such
a file as (stage, status). The `agent-ops migrate-tasks` command applies it
to every task file; like the command, it is deleted after the deploy
(ADR 0004)."""
from __future__ import annotations

import json
from pathlib import Path

from dispatcher import state
from dispatcher.models import IMPLEMENT_PICK
from dispatcher.state import Stage
from dispatcher.status import Cause, FailReason, HumanReason, Kind, Status, to_json

# The legacy park values (the `park` field).
PARK_HUMAN = "parked"
PARK_CI = "awaiting-ci"
PARK_WAKE = "unpark-requested"
PARK_LOGIN = "parked-login"
PARK_REVIEW = "awaiting-review"

# The legacy stage values the pipeline no longer has.
GATE = "awaiting-plan-review"
_RETIRED = {"blocked": Stage.IMPLEMENT, "stalled-on-budget": Stage.SPEC}
_ENDED = {"failed": Kind.FAILED, "done": Kind.DONE, "canceled": Kind.CANCELED}
# The legacy park_reason spellings.
_HUMAN_REASONS = {"decision": HumanReason.DECISION, "stalled": HumanReason.STALLED,
                  "loop cap": HumanReason.LOOP_CAP, "bad signal": HumanReason.BAD_SIGNAL,
                  "config": HumanReason.CONFIG, "operator": HumanReason.OPERATOR,
                  "": HumanReason.OPERATOR}
ACCESS = "access"
_REQUEST_PARKS = {"answers": Kind.PARKED_ANSWERS, "plan-approval": Kind.PARKED_REVIEW}


def legacy_status(d: dict) -> tuple[Stage, Status]:
    """(stage, status) of a task file stored with the park fields. Raises
    ValueError on a shape the table does not classify."""
    stage = d["stage"]
    if stage in _ENDED:
        return _ended(d)
    if stage in _RETIRED:
        return _retired(d)
    at = Stage.PLAN if stage == GATE else Stage(stage)
    if d.get("park") == PARK_WAKE:
        return at, _woken(d)
    if d.get("park"):
        return at, _parked(d)
    return _live(d)


def _ended(d: dict) -> tuple[Stage, Status]:
    """A failed, canceled or done task, whatever park it still carries. A
    remembered stage reads as a session crash, a task crash included: no
    legacy field holds the reason."""
    if d["stage"] == "failed" and d.get("crashed_stage"):
        return Stage(d["crashed_stage"]), Status(Kind.FAILED, reason=FailReason.SESSION_CRASH)
    kind = _ENDED[d["stage"]]
    stage = Stage.PR_OPEN if d.get("pr_number") else Stage.QUEUED
    return stage, Status(kind, reason=FailReason.UNKNOWN if kind is Kind.FAILED else None)


def _retired(d: dict) -> tuple[Stage, Status]:
    stage = Stage.PR_OPEN if d.get("pr_number") else _RETIRED[d["stage"]]
    note = f"migrated from legacy {d['stage']} stage"
    return stage, Status(Kind.PARKED_HUMAN, reason=HumanReason.OPERATOR, note=note)


def _woken(d: dict) -> Status:
    """A queued wake keeps what it would be still parked: its origin."""
    if d.get("crashed_stage"):
        return Status(Kind.WAKING, origin=Status(Kind.FAILED, reason=FailReason.SESSION_CRASH),
                      cause=Cause.RESUME)
    origin = _parked(d)
    cause = Cause.ACCESS if origin.kind is Kind.PARKED_ACCESS else Cause.REPLY
    return Status(Kind.WAKING, origin=origin, cause=cause)


def _parked(d: dict) -> Status:
    park, note = d.get("park"), d.get("park_note", "")
    if park == PARK_LOGIN:
        return Status(Kind.HELD_LOGIN, note=note)
    if park == PARK_CI:
        return _parked_ci(d)
    request = d.get("operator_request")
    if request is not None:
        return Status(_request_park(request), note=note, path=request.get("path", ""),
                      fingerprint=request.get("fingerprint", ""))
    if d.get("park_reason") == ACCESS:
        return Status(Kind.PARKED_ACCESS, note=note)
    return _parked_human(d)


def _request_park(request: dict) -> Kind:
    kind = request.get("kind")
    if kind not in _REQUEST_PARKS:
        raise ValueError(f"unrecognized operator_request kind {kind!r}")
    return _REQUEST_PARKS[kind]


def _parked_ci(d: dict) -> Status:
    if d.get("ci_run_id", 0) <= 0:
        raise ValueError(f"task {_name(d)}: awaiting-ci with no run id")
    return Status(Kind.PARKED_CI, run_id=d["ci_run_id"])


def _parked_human(d: dict) -> Status:
    park, reason = d.get("park"), d.get("park_reason", "")
    if park not in (PARK_HUMAN, PARK_WAKE) or reason not in _HUMAN_REASONS:
        raise ValueError(f"task {_name(d)}: unclassified park {park!r} reason {reason!r}")
    return Status(Kind.PARKED_HUMAN, reason=_HUMAN_REASONS[reason], note=d.get("park_note", ""))


def _live(d: dict) -> tuple[Stage, Status]:
    """A task with no park field set."""
    stage = d["stage"]
    if d.get("unlaunched"):
        return Stage.QUEUED, Status(Kind.WAKING, cause=Cause.LAUNCH)
    if stage == Stage.IMPLEMENT.value and _implement_done(d):
        return Stage.REVIEW, Status(Kind.WAKING, cause=Cause.NEXT_STAGE)
    if stage == Stage.PR_OPEN.value:
        return Stage.PR_OPEN, _at_pr(d)
    if stage == GATE:
        return Stage.PLAN, _at_gate(d)
    return Stage(stage), Status(Kind.RUNNING)


def _implement_done(d: dict) -> bool:
    """The implement session reported done and review has not started: a
    provider ran implement and its pick is gone."""
    return bool(d.get("implement_providers")) and IMPLEMENT_PICK not in (d.get("picks") or {})


def _at_pr(d: dict) -> Status:
    if d.get("feedback_pending"):
        return Status(Kind.WAKING, origin=Status(Kind.PARKED_PR),
                      cause=Cause(f"attention({d.get('attention') or 'feedback'})"))
    return Status(Kind.PARKED_PR)


def _at_gate(d: dict) -> Status:
    """A live plan session at the review gate, held on its request; one that
    reworks the plan (its request disarmed) or was respawned runs."""
    request = d.get("operator_request")
    if request is None:
        return Status(Kind.RUNNING)
    if request.get("kind") != "plan-approval":
        raise ValueError(f"task {_name(d)}: at the plan gate with "
                         f"an {request.get('kind')} request")
    return Status(Kind.HELD_PLAN_APPROVAL, path=request.get("path", ""),
                  fingerprint=request.get("fingerprint", ""))


def _name(d: dict) -> str:
    return f"{d.get('repository', d.get('target', '?'))}#{d.get('issue', '?')}"


# ---- The state dir rewritten -------------------------------------------------

class MigrationError(ValueError):
    """A task file the migration cannot rewrite; no file was changed."""

    def __init__(self, path: Path, why: object):
        super().__init__(f"{path}: {why}")
        self.path = path


def migrate_state_dir(state_dir: str | Path) -> list[Path]:
    """Rewrite every legacy task file of `state_dir`, archived snapshots
    included, to (stage, status); the files rewritten. Every file is
    planned before any is written, so a file that cannot be read or
    classified raises MigrationError naming it and changes nothing. A file
    already in the new shape is left alone: a second run, or a run after
    one killed midway, writes only what is still legacy."""
    plan = _plan(Path(state_dir))
    for p, task in plan:
        state.save_in_place(p, task)
    return [p for p, _ in plan]


def pending(state_dir: str | Path) -> list[Path]:
    """The task files still in the legacy shape, archived snapshots
    included: the files migrate_state_dir would rewrite. A file that
    cannot be read or classified raises MigrationError naming it, so a
    guard on this fails closed."""
    return [p for p, _ in _plan(Path(state_dir))]


def _plan(root: Path) -> list[tuple[Path, state.TaskState]]:
    return [(p, task) for p in _task_files(root) if (task := _planned(p)) is not None]


def _legacy(d: object) -> bool:
    """The one shape check: a task written before the status has none."""
    return isinstance(d, dict) and "status" not in d


def _task_files(root: Path) -> list[Path]:
    """The task files, a legacy `task-<issue>.json` twin among them, and
    the archived snapshots."""
    return sorted(root.glob("task-*.json")) + sorted(root.glob("artifacts/*/task.json"))


def _planned(p: Path) -> state.TaskState | None:
    """The task `p` is rewritten to, validated as a save validates it; None
    when it is in the new shape already."""
    try:
        d = json.loads(p.read_text())
        if not _legacy(d):
            state.from_dict(d)   # read as the dispatcher reads it
            return None
        return state.validated(_converted(d))
    except (ValueError, KeyError, TypeError) as e:   # JSONDecodeError is a ValueError
        raise MigrationError(p, e) from e


def _converted(d: dict) -> state.TaskState:
    stage, status = legacy_status(d)
    return state.from_dict({**d, "stage": stage.value, "status": to_json(status)})
