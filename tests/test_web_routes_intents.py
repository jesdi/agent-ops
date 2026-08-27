"""Session-backed actions: intent files, 202 accepted, never optimistic."""
from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app


def rig(tmp_path):
    fake = FakeSources()
    fake.tasks_list = [make_task(issue=7)]
    return fake, TestClient(create_app(make_config(tmp_path), fake))


def test_reply_writes_intent_and_returns_202(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/7/reply", headers=HEADERS,
                    json={"text": "use main, not master"})
    assert r.status_code == 202
    assert r.json() == {"status": "pending",
                        "intent": "1753430000000-7-reply.json"}
    assert fake.intents == [("reply", 7, {"text": "use main, not master"},
                             "jesdi@github")]


def test_reply_empty_text_422(tmp_path):
    fake, client = rig(tmp_path)
    assert client.post("/api/task/7/reply", headers=HEADERS,
                       json={"text": ""}).status_code == 422
    assert fake.intents == []


def test_reply_is_accepted_for_an_unclaimed_issue(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/999/reply", headers=HEADERS,
                    json={"text": "pre-brief"})
    assert r.status_code == 202
    assert fake.intents == [("reply", 999, {"text": "pre-brief"},
                             "jesdi@github")]


def test_park_still_requires_a_real_task(tmp_path):
    fake, client = rig(tmp_path)
    assert client.post("/api/task/999/park", headers=HEADERS,
                       json={}).status_code == 404


def test_park_kill_resume(tmp_path):
    fake, client = rig(tmp_path)
    assert client.post("/api/task/7/park", headers=HEADERS,
                       json={}).status_code == 202
    assert client.post("/api/task/7/kill", headers=HEADERS,
                       json={}).status_code == 202
    assert client.post("/api/task/7/resume", headers=HEADERS,
                       json={"text": "go on"}).status_code == 202
    assert [(i[0], i[2]) for i in fake.intents] == [
        ("park", {}), ("kill", {}), ("resume", {"text": "go on"})]


def test_cancel_writes_intent_for_a_live_task(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/7/cancel", headers=HEADERS, json={})
    assert r.status_code == 202
    assert fake.intents == [("cancel", 7, {}, "jesdi@github")]


def test_cancel_accepts_an_unclaimed_backlog_issue_with_target(tmp_path):
    # A backlog card has no task file; the target rides the payload so the
    # dispatcher can still retire the board card and close the issue.
    fake, client = rig(tmp_path)
    r = client.post("/api/task/999/cancel", headers=HEADERS,
                    json={"target": "portfolio_eval"})
    assert r.status_code == 202
    assert fake.intents == [("cancel", 999, {"target": "portfolio_eval"},
                             "jesdi@github")]


def test_retry_validates_against_quarantine_not_tasks(tmp_path):
    fake, client = rig(tmp_path)
    # issue 12 has no task-12.json but IS quarantined -> allowed
    fake.quarantine = [{"target": "alpha", "task_issue": 12,
                        "blocker_repo": "r", "blocker_issue": 1,
                        "fingerprint": "f", "created_at": "c"}]
    assert client.post("/api/task/12/retry", headers=HEADERS,
                       json={}).status_code == 202
    # issue 7 has a task but no quarantine record -> 404 for retry
    assert client.post("/api/task/7/retry", headers=HEADERS,
                       json={}).status_code == 404


def test_pending_intents_listing(tmp_path):
    fake, client = rig(tmp_path)
    fake.pending = [{"action": "reply", "issue": 7,
                     "actor": "jesdi@github",
                     "created_at": "2026-07-25T10:00:00+00:00"}]
    body = client.get("/api/pending-intents", headers=HEADERS).json()
    assert body == {"intents": fake.pending}


def test_intent_roundtrip_through_real_sources(tmp_path):
    """Web writes the intent exactly as dispatcher/intents.py defines it."""
    import json
    from dispatcher import state
    from tests.webfakes import make_config
    from web.sources import Sources
    from tests.test_web_sources import FakeGitHub, FakeSessions, FakeClock

    cfg = make_config(tmp_path)
    src = Sources(cfg, FakeSessions(), FakeGitHub(), clock=FakeClock(),
                  systemctl=("true",))
    state.save(tmp_path, make_task(issue=7))
    client = TestClient(create_app(cfg, src))
    r = client.post("/api/task/7/reply", headers=HEADERS,
                    json={"text": "hi"})
    assert r.status_code == 202
    path = tmp_path / "intents" / r.json()["intent"]
    d = json.loads(path.read_text())
    assert d["action"] == "reply" and d["issue"] == 7
    assert d["payload"] == {"text": "hi"}
    assert d["actor"] == "jesdi@github"
