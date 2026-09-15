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
    r = client.post("/api/task/alpha/7/reply", headers=HEADERS,
                    json={"text": "use main, not master"})
    assert r.status_code == 202
    assert r.json() == {"status": "pending",
                        "intent": "1753430000000-alpha-7-reply.json"}
    assert fake.intents == [("reply", "alpha", 7,
                             {"text": "use main, not master"},
                             "jesdi@github")]


def test_reply_empty_text_422(tmp_path):
    fake, client = rig(tmp_path)
    assert client.post("/api/task/alpha/7/reply", headers=HEADERS,
                       json={"text": ""}).status_code == 422
    assert fake.intents == []


def test_reply_is_accepted_for_an_unclaimed_issue(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/999/reply", headers=HEADERS,
                    json={"text": "pre-brief"})
    assert r.status_code == 202
    assert fake.intents == [("reply", "alpha", 999, {"text": "pre-brief"},
                             "jesdi@github")]


def test_park_still_requires_a_real_task(tmp_path):
    fake, client = rig(tmp_path)
    assert client.post("/api/task/alpha/999/park", headers=HEADERS,
                       json={}).status_code == 404


def test_park_kill_resume(tmp_path):
    fake, client = rig(tmp_path)
    assert client.post("/api/task/alpha/7/park", headers=HEADERS,
                       json={}).status_code == 202
    assert client.post("/api/task/alpha/7/kill", headers=HEADERS,
                       json={}).status_code == 202
    assert client.post("/api/task/alpha/7/resume", headers=HEADERS,
                       json={"text": "go on"}).status_code == 202
    assert [(i[0], i[3]) for i in fake.intents] == [
        ("park", {}), ("kill", {}), ("resume", {"text": "go on"})]


def test_resume_accepts_model_and_usage_override(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/7/resume", headers=HEADERS,
                    json={"model": "anthropic/claude-opus-5",
                          "bypass_usage": True})
    assert r.status_code == 202
    assert fake.intents[-1][3] == {
        "model": "anthropic/claude-opus-5", "bypass_usage": True}


def test_resume_rejects_unconfigured_model(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/7/resume", headers=HEADERS,
                    json={"model": "unknown/model"})
    assert r.status_code == 422
    assert fake.intents == []


def test_force_run_arms_an_active_task_without_resuming_it(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/7/run", headers=HEADERS,
                    json={"model": "anthropic/claude-opus-5",
                          "bypass_usage": True})
    assert r.status_code == 200
    assert fake.execution_overrides[("alpha", 7)] == (
        "anthropic/claude-opus-5", True)
    assert fake.intents == []
    assert fake.appended[-1][:3] == ("execution-forced", "alpha", 7)


def test_force_run_resumes_a_parked_task(tmp_path):
    from dataclasses import replace
    from dispatcher.state import PARK_HUMAN
    fake, client = rig(tmp_path)
    fake.tasks_list = [replace(fake.tasks_list[0], park=PARK_HUMAN)]
    r = client.post("/api/task/alpha/7/run", headers=HEADERS,
                    json={"model": "anthropic/claude-opus-5",
                          "bypass_usage": True})
    assert r.status_code == 202
    assert fake.intents[-1][0] == "resume"
    assert fake.intents[-1][3] == {
        "model": "anthropic/claude-opus-5", "bypass_usage": True}


def test_force_run_arms_an_unclaimed_queue_candidate(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = []
    fake.rank["alpha"] = ([{
        "number": 9, "title": "Queued", "url": "u", "status": "Ready",
        "labels": ["auto"], "blocked": False, "score": 2, "boost": 0,
    }], "now", False)
    r = client.post("/api/task/alpha/9/run", headers=HEADERS,
                    json={"model": "anthropic/claude-opus-5",
                          "bypass_usage": True})
    assert r.status_code == 200
    assert fake.execution_overrides[("alpha", 9)] == (
        "anthropic/claude-opus-5", True)


def test_force_run_rejects_missing_or_stale_queue_work(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = []

    missing = client.post("/api/task/alpha/9/run", headers=HEADERS, json={})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "no runnable task alpha/9"

    fake.rank["alpha"] = ([], "now", True)
    stale = client.post("/api/task/alpha/9/run", headers=HEADERS, json={})
    assert stale.status_code == 409
    assert stale.json()["detail"] == "queue for 'alpha' is stale"


def test_force_run_rejects_unknown_or_terminal_work(tmp_path):
    from dataclasses import replace
    from dispatcher.state import Stage
    fake, client = rig(tmp_path)
    assert client.post("/api/task/alpha/7/run", headers=HEADERS,
                       json={"model": "unknown/model"}).status_code == 422
    fake.tasks_list = [replace(fake.tasks_list[0], stage=Stage.DONE)]
    assert client.post("/api/task/alpha/7/run", headers=HEADERS,
                       json={}).status_code == 422


def test_cancel_writes_intent_for_a_live_task(tmp_path):
    fake, client = rig(tmp_path)
    r = client.post("/api/task/alpha/7/cancel", headers=HEADERS, json={})
    assert r.status_code == 202
    assert fake.intents == [("cancel", "alpha", 7, {}, "jesdi@github")]


def test_cancel_accepts_an_unclaimed_backlog_issue(tmp_path):
    # A backlog card has no task file; the target in the path is all the
    # dispatcher needs to retire the board card and close the issue.
    fake, client = rig(tmp_path)
    r = client.post("/api/task/portfolio_eval/999/cancel", headers=HEADERS,
                    json={})
    assert r.status_code == 202
    assert fake.intents == [("cancel", "portfolio_eval", 999, {},
                             "jesdi@github")]


def test_retry_validates_against_quarantine_not_tasks(tmp_path):
    fake, client = rig(tmp_path)
    # issue 12 has no task-12.json but IS quarantined -> allowed
    fake.quarantine = [{"target": "alpha", "task_issue": 12,
                        "blocker_repo": "r", "blocker_issue": 1,
                        "fingerprint": "f", "created_at": "c"}]
    assert client.post("/api/task/alpha/12/retry", headers=HEADERS,
                       json={}).status_code == 202
    # issue 7 has a task but no quarantine record -> 404 for retry
    assert client.post("/api/task/alpha/7/retry", headers=HEADERS,
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
    r = client.post("/api/task/alpha/7/reply", headers=HEADERS,
                    json={"text": "hi"})
    assert r.status_code == 202
    path = tmp_path / "intents" / r.json()["intent"]
    d = json.loads(path.read_text())
    assert d["action"] == "reply" and d["issue"] == 7
    assert d["payload"] == {"text": "hi"}
    assert d["actor"] == "jesdi@github"
