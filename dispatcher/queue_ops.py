"""Pure queue guard rules, shared by the Telegram commands and the web
console API: one rule set, one test suite, two front doors. plan_* take a
rank row (github.rank_rows shape) and return a QueuePlan; apply_plan is
the single executor, preserving /next's boost-first failure ordering.
No I/O in the plan functions."""
from __future__ import annotations

from dataclasses import dataclass

NEXT_BOOST = 99


@dataclass(frozen=True)
class QueuePlan:
    ok: bool
    reason: str = ""                  # human message (reject reason or success summary)
    set_boost: int | None = None
    set_ready: bool = False
    add_auto: bool = False


def plan_boost(row: dict, amount: int) -> QueuePlan:
    current = row.get("boost", 0)
    new = current + amount
    return QueuePlan(ok=True, reason=f"#{row['number']} boost {current} → {new}",
                     set_boost=new)


def plan_next(row: dict, force: bool) -> QueuePlan:
    issue = row["number"]
    if row["blocked"]:
        return QueuePlan(ok=False, reason=(
            f"#{issue} is blocked — resolve its blockers first "
            "(blocked issues cannot be forced)"))
    if row.get("status") == "In progress":
        # The board is the double-dispatch guard: flipping an In-progress
        # issue back to Ready would let a pass claim work already in
        # flight. Never forceable.
        return QueuePlan(ok=False, reason=(
            f"#{issue} is already In progress — work on it is already in "
            "flight (in-progress issues cannot be forced)"))
    problems = []
    if row.get("status") != "Ready":
        problems.append(f"status is {row.get('status') or 'unset'}, not Ready")
    if "auto" not in row.get("labels", []):
        problems.append("missing the auto label")
    if problems and not force:
        return QueuePlan(ok=False, reason=(
            f"#{issue} is not eligible: " + "; ".join(problems) + ".\n"
            f"Send /next {issue} force to make it eligible and enqueue."))
    # set_boost FIRST in apply_plan: it is the mutation most likely to fail
    # (the Boost field may not exist on the board yet), and failing before
    # status/label keeps a failed /next a clean no-op rather than a
    # half-eligible issue.
    return QueuePlan(
        ok=True,
        reason=f"#{issue} enqueued at the head (boost {NEXT_BOOST})",
        set_boost=NEXT_BOOST,
        set_ready=row.get("status") != "Ready",
        add_auto="auto" not in row.get("labels", []))


def plan_ready(row: dict) -> QueuePlan:
    issue = row["number"]
    if row.get("status") == "In progress":
        return QueuePlan(ok=False, reason=(
            f"#{issue} is already In progress — work on it is already in "
            "flight"))
    if row["blocked"]:
        return QueuePlan(ok=False, reason=(
            f"#{issue} is blocked — resolve its blockers first"))
    if row.get("status") == "Ready":
        return QueuePlan(ok=False, reason=f"#{issue} is already Ready")
    return QueuePlan(ok=True, reason=f"#{issue} marked Ready", set_ready=True)


def apply_plan(github, target, issue: int, plan: QueuePlan) -> None:
    if not plan.ok:
        return
    if plan.set_boost is not None:
        github.set_boost(target, issue, plan.set_boost)
    if plan.set_ready:
        github.set_status(target, issue, target.status_ready_option_id)
    if plan.add_auto:
        github.add_label(target, issue, "auto")
