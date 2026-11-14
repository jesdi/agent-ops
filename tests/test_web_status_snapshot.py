"""The console API of a task in each status kind is the JSON it was before
the status was stored (#132 ticket 06): the read model derives today's
fields until the web ticket changes them."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from dispatcher.state import (NO_SLOT, PARK_CI, PARK_HUMAN, PARK_LOGIN, PARK_REVIEW,
                              PARK_WAKE, WHY_ACCESS, WHY_DECISION, WHY_LOOP_CAP,
                              AnswersRequest, PlanApprovalRequest, Stage)
from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app

SNAPSHOT = Path(__file__).parent / "fixtures" / "status-api-snapshot.json"
GATE_PAGE = PlanApprovalRequest(".agent/review.html", fingerprint="ab")
ANSWERS_PAGE = AnswersRequest(".agent/questions.html", fingerprint="cd")

# One task per status kind (and per waking cause and failure the console
# tells apart), in the stored shape of its time.
TASKS = {
    1: dict(stage=Stage.IMPLEMENT, slot=0),                                  # running
    2: dict(stage=Stage.AWAITING_PLAN_REVIEW, slot=1,
            operator_request=GATE_PAGE),                                     # held(plan-approval)
    3: dict(stage=Stage.SPEC, slot=2, park=PARK_LOGIN, park_note="login",
            park_msg_id=5),                                                  # held(login)
    4: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, park=PARK_HUMAN,
            park_reason=WHY_DECISION, park_note="which db?"),                # parked(human)
    5: dict(stage=Stage.PLAN, slot=NO_SLOT, park=PARK_HUMAN, park_reason=WHY_DECISION,
            park_note="answer these", park_msg_id=8,
            operator_request=ANSWERS_PAGE),                                  # parked(answers)
    6: dict(stage=Stage.QUEUED, slot=NO_SLOT, park=PARK_HUMAN, park_reason=WHY_ACCESS,
            park_note="no access: https://x"),                               # parked(access)
    7: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, park=PARK_CI, ci_run_id=99),  # parked(ci)
    8: dict(stage=Stage.AWAITING_PLAN_REVIEW, slot=NO_SLOT, park=PARK_REVIEW,
            park_note="plan ready for review", operator_request=GATE_PAGE),  # parked(review)
    9: dict(stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=12),                # parked(pr)
    10: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, park=PARK_WAKE,
             park_reason=WHY_DECISION, park_note="which db?"),               # waking(reply)
    11: dict(stage=Stage.IMPLEMENT, slot=3, implement_providers=["anthropic"]),  # waking(next-stage)
    12: dict(stage=Stage.QUEUED, slot=4, unlaunched=True),                   # waking(launch)
    13: dict(stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=13, feedback_pending=True,
             attention="check-failed"),                                      # waking(attention)
    14: dict(stage=Stage.FAILED, slot=NO_SLOT, crashed_stage="implement"),   # failed(session-crash)
    15: dict(stage=Stage.FAILED, slot=NO_SLOT, crashed_stage="spec"),        # failed(task-crash)
    16: dict(stage=Stage.FAILED, slot=NO_SLOT),                              # failed(killed)
    17: dict(stage=Stage.CANCELED, slot=NO_SLOT),                            # canceled
    18: dict(stage=Stage.DONE, slot=NO_SLOT, pr_number=3,
             done_at="2026-07-25T12:00:00+00:00"),                           # done
    19: dict(stage=Stage.REVIEW, slot=NO_SLOT, park=PARK_WAKE,
             crashed_stage="review"),                                        # waking(resume) from failed
    20: dict(stage=Stage.AWAITING_PLAN_REVIEW, slot=NO_SLOT, park=PARK_WAKE,
             park_note="plan ready for review", operator_request=GATE_PAGE),  # waking from parked(review)
    21: dict(stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=21, park=PARK_HUMAN,
             park_reason=WHY_LOOP_CAP, park_note="ci loop exceeded"),        # parked(human) at pr-open
}


def _api(tmp_path) -> dict:
    fake = FakeSources()
    client = TestClient(create_app(make_config(tmp_path, capacity=4), fake))
    fake.tasks_list = [make_task(issue=n, **kw) for n, kw in TASKS.items()]
    board = client.get("/api/board", headers=HEADERS).json()
    return {"board": {"columns": board["columns"], "capacity": board["capacity"]},
            "tasks": {str(n): client.get(f"/api/task/alpha/{n}", headers=HEADERS).json()
                      for n in TASKS}}


def test_the_api_of_each_status_kind_is_unchanged(tmp_path):
    # The snapshot was written by this test against the code before the change.
    assert _api(tmp_path) == json.loads(SNAPSHOT.read_text())
