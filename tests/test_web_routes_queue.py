"""Queue view + synchronous queue actions through queue_ops."""
from fastapi.testclient import TestClient

from tests.webfakes import (FakeSources, HEADERS, make_config, make_target,
                            make_task)
from web.app import create_app


ROW = {"number": 9, "title": "Add feature", "url": "https://x/9",
       "status": "Ready", "labels": ["auto"], "blocked": False,
       "score": 4.2, "boost": 0}


def rig(tmp_path, targets=None):
    fake = FakeSources()
    cfg = make_config(tmp_path, targets=targets)
    return fake, TestClient(create_app(cfg, fake))


def test_queue_view_marks_in_flight_and_stale(tmp_path):
    fake, client = rig(tmp_path)
    fake.rank["alpha"] = ([dict(ROW)], "2026-07-25T10:00:00+00:00", True)
    fake.tasks_list = [make_task(issue=9)]
    body = client.get("/api/queue", headers=HEADERS).json()
    tq = body["targets"][0]
    assert tq["target"] == "alpha" and tq["stale"] is True
    assert tq["as_of"] == "2026-07-25T10:00:00+00:00"
    assert tq["rows"][0]["in_flight"] is True
    assert tq["rows"][0]["score"] == 4.2


def test_boost_applies_plan_and_logs_event(tmp_path):
    fake, client = rig(tmp_path)
    fake.rank["alpha"] = ([dict(ROW)], "t", False)
    r = client.post("/api/queue/boost", headers=HEADERS,
                    json={"issue": 9, "amount": 3})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    target_name, issue, plan = fake.applied_plans[0]
    assert (target_name, issue) == ("alpha", 9)
    assert plan.set_boost == 3
    event, target, issue, actor, _ = fake.appended[0]
    assert event == "queue-boost" and actor == "jesdi@github"


def test_boost_unknown_issue_404(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/queue/boost", headers=HEADERS,
                    json={"issue": 999, "amount": 1})
    assert r.status_code == 404


def test_boost_ambiguous_across_targets_409(tmp_path):
    targets = [make_target("alpha"), make_target("beta", "jesdi/beta")]
    fake, client = rig(tmp_path, targets=targets)
    fake.rank["alpha"] = ([dict(ROW)], "t", False)
    fake.rank["beta"] = ([dict(ROW)], "t", False)
    r = client.post("/api/queue/boost", headers=HEADERS,
                    json={"issue": 9, "amount": 1})
    assert r.status_code == 409
    assert fake.applied_plans == []


def test_rejected_plan_is_422_with_reason(tmp_path):
    fake, client = rig(tmp_path)
    blocked = dict(ROW, blocked=True)
    fake.rank["alpha"] = ([blocked], "t", False)
    r = client.post("/api/queue/next", headers=HEADERS,
                    json={"issue": 9, "force": True})
    assert r.status_code == 422
    assert r.json()["detail"]  # queue_ops' reason surfaces verbatim
    assert fake.applied_plans == []


def test_next_and_ready_happy_paths(tmp_path):
    fake, client = rig(tmp_path)
    row = dict(ROW, status="Backlog", labels=[])
    fake.rank["alpha"] = ([row], "t", False)
    r = client.post("/api/queue/next", headers=HEADERS,
                    json={"issue": 9, "force": True})
    assert r.status_code == 200
    r = client.post("/api/queue/ready", headers=HEADERS,
                    json={"issue": 9})
    assert r.status_code == 200
    assert [e[0] for e in fake.appended] == ["queue-next", "queue-ready"]
