"""The console API of a task in each status kind is the JSON it was before
the status was stored (#132 ticket 06): the read model derives today's
fields until the web ticket changes them."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from dispatcher.state import NO_SLOT, AnswersRequest, PlanApprovalRequest, Stage
from tests.statuses import (CANCELED, DONE, PR, access, answers, ci, failed, held, human,
                            login, review, waking)
from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app

SNAPSHOT = Path(__file__).parent / "fixtures" / "status-api-snapshot.json"
GATE_PAGE = PlanApprovalRequest(".agent/review.html", fingerprint="ab")
ANSWERS_PAGE = AnswersRequest(".agent/questions.html", fingerprint="cd")
QUESTION = human("decision", "which db?")
PLAN_READY = review(GATE_PAGE, "plan ready for review")

# One task per status kind (and per waking cause and failure the console
# tells apart). The snapshot was written from the same tasks stored with the
# park fields, before the status was stored.
TASKS = {
    1: dict(stage=Stage.IMPLEMENT, slot=0),                                  # running
    2: dict(stage=Stage.PLAN, slot=1, status=held(GATE_PAGE)),
    3: dict(stage=Stage.SPEC, slot=2, status=login("login"), park_msg_id=5),
    4: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, status=QUESTION),
    5: dict(stage=Stage.PLAN, slot=NO_SLOT, status=answers(ANSWERS_PAGE, "answer these"),
            park_msg_id=8),
    6: dict(stage=Stage.QUEUED, slot=NO_SLOT, status=access("no access: https://x")),
    7: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, status=ci(99)),
    8: dict(stage=Stage.PLAN, slot=NO_SLOT, status=PLAN_READY),
    9: dict(stage=Stage.PR_OPEN, slot=NO_SLOT, status=PR, pr_number=12),
    10: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, status=waking(QUESTION, "reply")),
    11: dict(stage=Stage.REVIEW, slot=NO_SLOT, status=waking(cause="next-stage"),
             implement_providers=["anthropic"]),
    12: dict(stage=Stage.QUEUED, slot=NO_SLOT, status=waking(cause="launch")),
    13: dict(stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=13,
             status=waking(PR, "attention(check-failed)")),
    14: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, status=failed("session-crash")),
    15: dict(stage=Stage.SPEC, slot=NO_SLOT, status=failed("task-crash")),
    16: dict(stage=Stage.IMPLEMENT, slot=NO_SLOT, status=failed("killed")),
    17: dict(stage=Stage.QUEUED, slot=NO_SLOT, status=CANCELED),
    18: dict(stage=Stage.PR_OPEN, slot=NO_SLOT, status=DONE, pr_number=3,
             done_at="2026-07-25T12:00:00+00:00"),
    19: dict(stage=Stage.REVIEW, slot=NO_SLOT, status=waking(failed(), "resume")),
    20: dict(stage=Stage.PLAN, slot=NO_SLOT, status=waking(PLAN_READY, "reply")),
    21: dict(stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=21,
             status=human("loop-cap", "ci loop exceeded")),
}

# The one change the stored status makes to the API (requirement 7): a task
# waiting to launch (review after implement, a claim the App held back)
# holds no capacity unit and no slot.
WAITS_TO_LAUNCH = (11, 12)


def _expected() -> dict:
    want = json.loads(SNAPSHOT.read_text())
    cards = [c for col in want["board"]["columns"] for c in col["cards"]]
    for n in WAITS_TO_LAUNCH:
        for card in [want["tasks"][str(n)]["card"],
                     *(c for c in cards if c["issue"] == n)]:
            card.update(consuming_capacity=False, slot=NO_SLOT)
    capacity = want["board"]["capacity"]
    capacity["slots_held"] = [s for s in capacity["slots_held"] if s not in (3, 4)]
    capacity.update(active=capacity["active"] - 2, slots_used=len(capacity["slots_held"]))
    return want


def _api(tmp_path) -> dict:
    fake = FakeSources()
    client = TestClient(create_app(make_config(tmp_path, capacity=4), fake))
    fake.tasks_list = [make_task(issue=n, **kw) for n, kw in TASKS.items()]
    board = client.get("/api/board", headers=HEADERS).json()
    return {"board": {"columns": board["columns"], "capacity": board["capacity"]},
            "tasks": {str(n): client.get(f"/api/task/alpha/{n}", headers=HEADERS).json()
                      for n in TASKS}}


def test_the_api_of_each_status_kind_is_unchanged(tmp_path):
    assert _api(tmp_path) == _expected()
