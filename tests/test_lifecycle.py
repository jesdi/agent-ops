"""dispatcher/lifecycle.py: the task lifecycle table (#132, ADR 0014)."""
import pytest

from dispatcher.lifecycle import (Kind, Status, legacy_view, occupies,
                                  operator_request)
from dispatcher.state import (AnswersRequest, PlanApprovalRequest, Stage,
                              TaskState)

ANSWERS = AnswersRequest(path="q.html", fingerprint="ab12")
PLAN = PlanApprovalRequest(path="review.html", fingerprint="cd34")


def task(stage=Stage.IMPLEMENT, **kw) -> TaskState:
    return TaskState(issue=363, repository="portfolio_eval", stage=stage, slot=0,
                     worktree="/wt", branch="agent/task-363", title="t",
                     updated_at="2026-11-13T00:00:00+00:00", **kw)


def human(reason, note=""):
    return Status(Kind.PARKED_HUMAN, reason=reason, note=note)


def waking(origin, cause):
    return Status(Kind.WAKING, origin=origin, cause=cause)


S = Stage
REVIEW_PARK = Status(Kind.PARKED_REVIEW, path="review.html", fingerprint="cd34")
ANSWERS_PARK = Status(Kind.PARKED_ANSWERS, path="q.html", fingerprint="ab12")

# The table of the scenario "migration command rewrites each legacy shape".
MIGRATION = [
    (dict(stage=S.IMPLEMENT), S.IMPLEMENT, Status(Kind.RUNNING)),
    (dict(park="parked", park_reason="decision", park_note="which DB?"),
     S.IMPLEMENT, human("decision", "which DB?")),
    (dict(park="parked"), S.IMPLEMENT, human("operator")),
    (dict(stage=S.QUEUED, park="parked", park_reason="access"),
     S.QUEUED, Status(Kind.PARKED_ACCESS)),
    (dict(stage=S.SPEC, park="parked", operator_request=ANSWERS), S.SPEC, ANSWERS_PARK),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="awaiting-review", operator_request=PLAN),
     S.PLAN, REVIEW_PARK),
    (dict(stage=S.AWAITING_PLAN_REVIEW, operator_request=PLAN), S.PLAN,
     Status(Kind.HELD_PLAN_APPROVAL, path="review.html", fingerprint="cd34")),
    (dict(park="awaiting-ci", ci_run_id=991), S.IMPLEMENT, Status(Kind.PARKED_CI, run_id=991)),
    (dict(stage=S.SPEC, park="parked-login"), S.SPEC, Status(Kind.HELD_LOGIN)),
    (dict(stage=S.PLAN, park="unpark-requested"), S.PLAN,
     waking(human("operator"), "reply")),
    (dict(park="unpark-requested", crashed_stage="implement"), S.IMPLEMENT,
     waking(Status(Kind.FAILED, reason="session-crash"), "resume")),
    (dict(stage=S.SPEC, park="unpark-requested", operator_request=ANSWERS), S.SPEC,
     waking(ANSWERS_PARK, "reply")),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="unpark-requested", operator_request=PLAN),
     S.PLAN, waking(REVIEW_PARK, "reply")),
    (dict(stage=S.QUEUED, park="unpark-requested", park_reason="access"), S.QUEUED,
     waking(Status(Kind.PARKED_ACCESS), "access")),
    (dict(stage=S.QUEUED, unlaunched=True), S.QUEUED, waking(None, "launch")),
    (dict(implement_providers=["anthropic"]), S.REVIEW, waking(None, "next-stage")),
    (dict(stage=S.PR_OPEN, feedback_pending=True, attention="conflict"), S.PR_OPEN,
     waking(Status(Kind.PARKED_PR), "attention(conflict)")),
    (dict(stage=S.PR_OPEN), S.PR_OPEN, Status(Kind.PARKED_PR)),
    (dict(stage=S.PR_OPEN, park="parked", park_reason="loop cap"), S.PR_OPEN,
     human("loop-cap")),
    (dict(stage=S.FAILED, crashed_stage="implement"), S.IMPLEMENT,
     Status(Kind.FAILED, reason="session-crash")),
    (dict(stage=S.FAILED, park="parked", pr_number=88), S.PR_OPEN,
     Status(Kind.FAILED, reason="unknown")),
    (dict(stage=S.FAILED, pr_number=88), S.PR_OPEN, Status(Kind.FAILED, reason="unknown")),
    (dict(stage=S.FAILED), S.QUEUED, Status(Kind.FAILED, reason="unknown")),
    (dict(stage=S.DONE, pr_number=88), S.PR_OPEN, Status(Kind.DONE)),
    (dict(stage=S.CANCELED), S.QUEUED, Status(Kind.CANCELED)),
    (dict(stage=S.BLOCKED), S.IMPLEMENT,
     human("operator", "migrated from legacy blocked stage")),
    (dict(stage=S.STALLED_ON_BUDGET), S.SPEC,
     human("operator", "migrated from legacy stalled-on-budget stage")),
]


@pytest.mark.parametrize("shape, stage, status", MIGRATION)
def test_the_view_reads_each_legacy_shape_as_the_migration_table_names(shape, stage, status):
    assert legacy_view(task(**shape)) == (stage, status)


def test_a_task_exposes_its_status_view():
    t = task(park="parked", park_reason="decision", park_note="which DB?")
    assert t.status == human("decision", "which DB?")


@pytest.mark.parametrize("shape", [
    dict(park="awaiting-ci", ci_run_id=0),
    dict(park="parked", park_reason="no such reason"),
    dict(stage=S.AWAITING_PLAN_REVIEW),
])
def test_a_shape_the_table_does_not_classify_is_refused(shape):
    with pytest.raises(ValueError):
        legacy_view(task(**shape))



@pytest.mark.parametrize("kind", list(Kind))
def test_occupies_exactly_running_and_held(kind):
    assert occupies(Status(kind)) is (kind in {
        Kind.RUNNING, Kind.HELD_PLAN_APPROVAL, Kind.HELD_LOGIN})


@pytest.mark.parametrize("shape, asked", [
    (dict(stage=S.AWAITING_PLAN_REVIEW, operator_request=PLAN), PLAN),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="awaiting-review", operator_request=PLAN), PLAN),
    (dict(stage=S.SPEC, park="parked", operator_request=ANSWERS), ANSWERS),
    (dict(stage=S.SPEC, park="unpark-requested", operator_request=ANSWERS), ANSWERS),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="unpark-requested", operator_request=PLAN),
     PLAN),
    (dict(park="unpark-requested", crashed_stage="implement"), None),
    (dict(stage=S.QUEUED, unlaunched=True), None),
    (dict(park="parked", park_reason="decision"), None),
    (dict(), None),
    (dict(stage=S.DONE, pr_number=88), None),
])
def test_operator_request_is_derived_from_the_status(shape, asked):
    got = operator_request(task(**shape))
    assert (got, getattr(got, "fingerprint", None)) == (
        asked, getattr(asked, "fingerprint", None))
