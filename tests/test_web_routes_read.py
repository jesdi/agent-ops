"""GET routes wired against FakeSources."""
from fastapi.testclient import TestClient

from dispatcher.budget import UsageSnapshot
from dispatcher.state import PARK_HUMAN, Stage
from tests.webfakes import (FakeSources, HEADERS, make_config, make_task)
from web.app import create_app


def rig(tmp_path):
    fake = FakeSources()
    client = TestClient(create_app(make_config(tmp_path), fake))
    return fake, client


def test_board_resolves_model_and_columns(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, stage=Stage.IMPLEMENT),
                       make_task(issue=8, stage=Stage.SPEC,
                                 park=PARK_HUMAN)]
    fake.attached.add(7)
    body = client.get("/api/board", headers=HEADERS).json()
    cols = {c["key"]: c for c in body["columns"]}
    card7 = cols["in-progress"]["cards"][0]
    assert card7["issue"] == 7 and card7["attached"] is True
    assert card7["model"]  # resolved via the config's model policy
    assert cols["parked"]["cards"][0]["issue"] == 8
    assert body["capacity"] == {"active": 1, "capacity": 2,
                                "slots_used": 2, "max_slots": 3}


def test_task_detail_and_404(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, pending_reply="please rebase",
                                 ci_run_id=42, effort=3,
                                 labels=("auto",))]
    fake.pane_tails[7] = "$ pytest -q\n3 passed"
    fake.alive.add(7)
    body = client.get("/api/task/7", headers=HEADERS).json()
    assert body["card"]["issue"] == 7
    assert body["pane_tail"].endswith("3 passed")
    assert body["session_alive"] is True
    assert body["pending_reply"] == "please rebase"
    assert body["ci_run_id"] == 42 and body["effort"] == 3
    assert body["labels"] == ["auto"]
    assert client.get("/api/task/999", headers=HEADERS).status_code == 404


def test_budget_base_threshold(tmp_path):
    fake, client = rig(tmp_path)
    fake.snapshot = UsageSnapshot(0.5, 120.0, "oauth")
    body = client.get("/api/budget", headers=HEADERS).json()
    assert body == {"utilization": 0.5, "minutes_to_reset": 120.0,
                    "source": "oauth", "would_spawn": True,
                    "threshold_applied": "base"}


def test_budget_reset_racing(tmp_path):
    fake, client = rig(tmp_path)
    fake.snapshot = UsageSnapshot(0.9, 10.0, "ccusage")
    body = client.get("/api/budget", headers=HEADERS).json()
    assert body["threshold_applied"] == "reset-racing"
    assert body["would_spawn"] is True  # 0.9 < racing_threshold 0.95


def test_budget_unavailable(tmp_path):
    fake, client = rig(tmp_path)
    fake.snapshot = UsageSnapshot(1.0, 0.0, "unavailable")
    body = client.get("/api/budget", headers=HEADERS).json()
    assert body["would_spawn"] is False
    assert body["threshold_applied"] == "n/a"


def test_failures_joins_blocker_state(tmp_path):
    fake, client = rig(tmp_path)
    fake.quarantine = [{"target": "alpha", "task_issue": 7,
                        "blocker_repo": "jesdi/agent-ops",
                        "blocker_issue": 31, "fingerprint": "abc",
                        "created_at": "2026-07-25T09:05:00"}]
    fake.fingerprints = [{"fingerprint": "abc", "repo": "jesdi/alpha",
                          "issue": 4, "when": "2026-07-25T09:00:00"}]
    fake.open_issues[("jesdi/agent-ops", 31)] = True
    body = client.get("/api/failures", headers=HEADERS).json()
    assert body["quarantined"][0]["blocker_open"] is True
    assert body["fingerprints"][0]["fingerprint"] == "abc"


def test_failures_blocker_unknown_is_null(tmp_path):
    fake, client = rig(tmp_path)
    fake.quarantine = [{"target": "alpha", "task_issue": 7,
                        "blocker_repo": "r", "blocker_issue": 1,
                        "fingerprint": "f", "created_at": "c"}]
    body = client.get("/api/failures", headers=HEADERS).json()
    assert body["quarantined"][0]["blocker_open"] is None


def test_history_respects_limit(tmp_path):
    fake, client = rig(tmp_path)
    fake.events = [{"ts": f"t{i}", "event": "claimed", "target": "alpha",
                    "issue": i, "stage": "spec", "model": "m",
                    "actor": "dispatcher", "detail": ""}
                   for i in range(10)]
    body = client.get("/api/history", headers=HEADERS,
                      params={"limit": 3}).json()
    assert [e["issue"] for e in body["events"]] == [7, 8, 9]


def test_task_spec_served_from_worktree(tmp_path):
    fake, client = rig(tmp_path)
    wt = tmp_path / "wt"
    spec = wt / "docs" / "superpowers" / "specs" / "x-design.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Widget spec\n\nbody")
    fake.tasks_list = [make_task(issue=7, worktree=str(wt),
                                 stage=Stage.AWAITING_SPEC_REVIEW,
                                 artifact=str(spec))]
    body = client.get("/api/task/7/spec", headers=HEADERS).json()
    assert body["path"] == "docs/superpowers/specs/x-design.md"
    assert body["markdown"].startswith("# Widget spec")


def test_task_spec_404s(tmp_path):
    fake, client = rig(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    outside = tmp_path / "evil.md"
    outside.write_text("nope")
    fake.tasks_list = [
        make_task(issue=1, worktree=str(wt)),                       # no artifact
        make_task(issue=2, worktree=str(wt),
                  artifact=str(wt / "gone.md")),                    # file missing
        make_task(issue=3, worktree=str(wt), artifact=str(outside)),  # escapes worktree
    ]
    for issue in (1, 2, 3, 999):                                    # 999: unknown task
        assert client.get(f"/api/task/{issue}/spec",
                          headers=HEADERS).status_code == 404


def test_task_spec_non_utf8_is_404(tmp_path):
    fake, client = rig(tmp_path)
    wt = tmp_path / "wt"
    spec = wt / "docs" / "superpowers" / "specs" / "bad-encoding.md"
    spec.parent.mkdir(parents=True)
    spec.write_bytes(b"\xff\xfe bad")
    fake.tasks_list = [make_task(issue=7, worktree=str(wt),
                                 stage=Stage.AWAITING_SPEC_REVIEW,
                                 artifact=str(spec))]
    assert client.get("/api/task/7/spec",
                      headers=HEADERS).status_code == 404


def test_task_history_returns_pane_history(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    fake.pane_histories[7] = "old line\nnewer line"
    body = client.get("/api/task/7/history", headers=HEADERS).json()
    assert body == {"text": "old line\nnewer line"}
    assert fake.history_calls == [(7, 2000)]  # default lines


def test_task_history_honours_lines_and_clamps(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    client.get("/api/task/7/history?lines=500", headers=HEADERS)
    client.get("/api/task/7/history?lines=99999", headers=HEADERS)
    assert fake.history_calls == [(7, 500), (7, 10000)]  # clamped to max


def test_task_history_404_for_unknown_task(tmp_path):
    fake, client = rig(tmp_path)
    assert client.get("/api/task/999/history",
                      headers=HEADERS).status_code == 404


def test_board_carries_next_claim_upcoming_and_timeline(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, stage=Stage.IMPLEMENT)]
    fake.heartbeat = {"started_at": "2026-08-01T12:25:00+00:00",
                      "finished_at": "2026-08-01T12:26:00+00:00",
                      "interval_minutes": 10}
    fake.rank["alpha"] = ([{"number": 73, "title": "t73", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 2.0, "boost": 0}],
                          "2026-08-01T12:00:00+00:00", False)
    # Use a timestamp guaranteed to be in the past so stage_timeline produces
    # an ongoing segment (now - claimed_at >= 1 s at any wall-clock time).
    fake.events = [{"ts": "2026-07-31T10:00:00+00:00", "event": "claimed",
                    "target": "alpha", "issue": 7, "stage": "queued",
                    "model": "", "actor": "dispatcher", "detail": ""}]
    body = client.get("/api/board", headers=HEADERS).json()
    assert body["next_claim"]["next_issue"] == 73
    assert [g["number"] for g in body["upcoming"]] == [73]
    card = [c for col in body["columns"] for c in col["cards"]][0]
    assert card["claimed_at"] == "2026-07-31T10:00:00+00:00"
    detail = client.get("/api/task/7", headers=HEADERS).json()
    assert detail["timeline"][0]["label"] == "queued"
    assert detail["timeline"][0]["ongoing"] is True


def test_board_next_claim_claims_paused(tmp_path):
    fake, client = rig(tmp_path)
    fake.heartbeat = {"started_at": "2026-08-01T12:25:00+00:00",
                      "finished_at": "2026-08-01T12:26:00+00:00",
                      "interval_minutes": 10}
    fake.rank["alpha"] = ([{"number": 73, "title": "t73", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 2.0, "boost": 0}],
                          "2026-08-01T12:00:00+00:00", False)
    fake._claims_paused = True
    body = client.get("/api/board", headers=HEADERS).json()
    assert body["next_claim"]["verdict"] == "claims-paused"
