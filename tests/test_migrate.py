"""dispatcher/migrate.py: a task file in the shape before the stored status
reads as (stage, status) — the table of the scenario "migration command
rewrites each legacy shape" (#132)."""
import pytest

from dispatcher.migrate import legacy_status
from dispatcher.state import Stage as S
from dispatcher.status import Kind, Status
from tests.statuses import PR, failed, human, waking

ANSWERS = {"kind": "answers", "path": "q.html", "fingerprint": "ab12"}
PLAN = {"kind": "plan-approval", "path": "review.html", "fingerprint": "cd34"}
REVIEW_PARK = Status(Kind.PARKED_REVIEW, path="review.html", fingerprint="cd34")
ANSWERS_PARK = Status(Kind.PARKED_ANSWERS, path="q.html", fingerprint="ab12")


def legacy(stage="implement", **kw) -> dict:
    return {"issue": 363, "repository": "portfolio_eval", "stage": stage, **kw}


MIGRATION = [
    (legacy(), S.IMPLEMENT, Status(Kind.RUNNING)),
    (legacy(park="parked", park_reason="decision", park_note="which DB?"),
     S.IMPLEMENT, human("decision", "which DB?")),
    (legacy(park="parked"), S.IMPLEMENT, human("operator")),
    (legacy("queued", park="parked", park_reason="access"), S.QUEUED, Status(Kind.PARKED_ACCESS)),
    (legacy("spec", park="parked", operator_request=ANSWERS), S.SPEC, ANSWERS_PARK),
    (legacy("awaiting-plan-review", park="awaiting-review", operator_request=PLAN),
     S.PLAN, REVIEW_PARK),
    (legacy("awaiting-plan-review", operator_request=PLAN), S.PLAN,
     Status(Kind.HELD_PLAN_APPROVAL, path="review.html", fingerprint="cd34")),
    (legacy("awaiting-plan-review"), S.PLAN, Status(Kind.RUNNING)),
    (legacy(park="awaiting-ci", ci_run_id=991), S.IMPLEMENT, Status(Kind.PARKED_CI, run_id=991)),
    (legacy("spec", park="parked-login"), S.SPEC, Status(Kind.HELD_LOGIN)),
    (legacy("plan", park="unpark-requested"), S.PLAN, waking(human("operator"), "reply")),
    (legacy(park="unpark-requested", crashed_stage="implement"), S.IMPLEMENT,
     waking(failed("session-crash"), "resume")),
    (legacy("spec", park="unpark-requested", operator_request=ANSWERS), S.SPEC,
     waking(ANSWERS_PARK, "reply")),
    (legacy("awaiting-plan-review", park="unpark-requested", operator_request=PLAN),
     S.PLAN, waking(REVIEW_PARK, "reply")),
    (legacy("queued", park="unpark-requested", park_reason="access"), S.QUEUED,
     waking(Status(Kind.PARKED_ACCESS), "access")),
    (legacy("queued", unlaunched=True), S.QUEUED, waking(None, "launch")),
    (legacy(implement_providers=["anthropic"]), S.REVIEW, waking(None, "next-stage")),
    (legacy(implement_providers=["anthropic"], picks={"implement": "anthropic/x"}),
     S.IMPLEMENT, Status(Kind.RUNNING)),
    (legacy("pr-open", feedback_pending=True, attention="conflict"), S.PR_OPEN,
     waking(PR, "attention(conflict)")),
    (legacy("pr-open"), S.PR_OPEN, PR),
    (legacy("pr-open", park="parked", park_reason="loop cap"), S.PR_OPEN, human("loop-cap")),
    (legacy("failed", crashed_stage="implement"), S.IMPLEMENT, failed("session-crash")),
    (legacy("failed", park="parked", pr_number=88), S.PR_OPEN, failed("unknown")),
    (legacy("failed", pr_number=88), S.PR_OPEN, failed("unknown")),
    (legacy("failed"), S.QUEUED, failed("unknown")),
    (legacy("done", pr_number=88), S.PR_OPEN, Status(Kind.DONE)),
    (legacy("canceled"), S.QUEUED, Status(Kind.CANCELED)),
    (legacy("blocked"), S.IMPLEMENT, human("operator", "migrated from legacy blocked stage")),
    (legacy("stalled-on-budget"), S.SPEC,
     human("operator", "migrated from legacy stalled-on-budget stage")),
]


@pytest.mark.parametrize("shape, stage, status", MIGRATION)
def test_each_legacy_shape_reads_as_the_migration_table_names(shape, stage, status):
    assert legacy_status(shape) == (stage, status)


@pytest.mark.parametrize("shape", [
    legacy(park="awaiting-ci", ci_run_id=0),
    legacy(park="parked", park_reason="no such reason"),
    legacy("awaiting-plan-review", operator_request=ANSWERS),
    legacy("spec", park="parked", operator_request={"kind": "nope", "path": "x"}),
    legacy("no-such-stage"),
])
def test_a_shape_the_table_does_not_classify_is_refused(shape):
    with pytest.raises(ValueError):
        legacy_status(shape)
