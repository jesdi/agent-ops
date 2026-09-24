"""GET routes wired against FakeSources."""
import json
import pytest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from dispatcher import messages as msgq
from dataclasses import replace

from dispatcher.state import PARK_HUMAN, PARK_WAKE, Stage
from tests.webfakes import (FakeSources, HEADERS, make_config, make_task)
from web.app import create_app


def _fresh_heartbeat(interval_minutes: int = 10) -> dict:
    """Heartbeat whose finished_at is 1 minute ago — always within the
    2*interval staleness window, regardless of wall-clock time of day."""
    now = datetime.now(timezone.utc)
    return {
        "started_at": (now - timedelta(minutes=2)).isoformat(),
        "finished_at": (now - timedelta(minutes=1)).isoformat(),
        "interval_minutes": interval_minutes,
    }


def rig(tmp_path):
    fake = FakeSources()
    client = TestClient(create_app(make_config(tmp_path), fake))
    return fake, client


def test_board_resolves_model_and_columns(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, stage=Stage.IMPLEMENT),
                       make_task(issue=8, stage=Stage.SPEC,
                                 park=PARK_HUMAN)]
    body = client.get("/api/board", headers=HEADERS).json()
    cols = {c["key"]: c for c in body["columns"]}
    card7 = cols["in-progress"]["cards"][0]
    assert card7["issue"] == 7
    assert "attached" not in card7
    assert card7["model"]  # resolved via the config's model policy
    assert cols["parked"]["cards"][0]["issue"] == 8
    # #8 is question-parked: it released both its capacity unit and its slot,
    # so the gauge reads 1/4 with exactly one segment lit.
    assert body["capacity"] == {"active": 1, "capacity": 2,
                                "slots_used": 1, "max_slots": 4,
                                "slots_held": [0]}


def _msg(mid, text, delivered=""):
    return msgq.Message(id=mid, text=text, actor="jesdi@github",
                        created_at="2026-08-12T10:00:00+00:00",
                        delivered_at=delivered)


def _intent(action, issue, text, iid="i1"):
    return {"action": action, "issue": issue, "actor": "jesdi@github",
            "created_at": "2026-08-12T10:07:00+00:00", "id": iid,
            "text": text}


def test_task_detail_and_404(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, ci_run_id=42, effort=3,
                                 labels=("auto",))]
    fake.pane_tails[7] = "$ pytest -q\n3 passed"
    fake.alive.add(7)
    fake.messages_by_key[("alpha", 7)] = [
        _msg("m1", "use oauth", delivered="2026-08-12T10:05:00+00:00"),
        _msg("m2", "and rebase")]
    fake.pending = [
        _intent("reply", 7, "one more thing"),
        _intent("reply", 8, "another task's reply", iid="i2"),  # wrong issue
        _intent("kill", 7, "", iid="i3"),                       # not a message
    ]
    body = client.get("/api/task/alpha/7", headers=HEADERS).json()
    assert body["card"]["issue"] == 7
    assert body["pane_tail"].endswith("3 passed")
    assert body["session_alive"] is True
    assert body["ci_run_id"] == 42 and body["effort"] == 3
    assert body["labels"] == ["auto"]
    # The thread the console renders: the durable queue plus this issue's
    # not-yet-drained reply intents, and nothing else.
    assert [(m["text"], m["state"]) for m in body["messages"]] == [
        ("use oauth", "delivered"),
        ("and rebase", "queued"),
        ("one more thing", "sending")]
    assert body["card"]["undelivered_messages"] == 1
    assert body["delivery_contract"] == (
        "will deliver at the next session boundary — this session is still "
        "running")
    assert client.get("/api/task/alpha/999", headers=HEADERS).status_code == 404


def test_task_detail_scoped_by_target(tmp_path):
    """Same issue number claimed on two different targets: the route must
    disambiguate on the full (target, issue) pair, not the bare issue
    number — this is the scenario the old /api/task/{issue} route could
    never resolve correctly."""
    from tests.webfakes import make_target
    fake = FakeSources()
    cfg = make_config(tmp_path, targets=[
        make_target("agent_ops", "jesdi/agent-ops"),
        make_target("portfolio_eval", "jesdi/portfolio-eval")])
    client = TestClient(create_app(cfg, fake))
    fake.tasks_list = [
        make_task(issue=42, target="agent_ops",
                  worktree="/tmp/worktrees/agent_ops/42"),
        make_task(issue=42, target="portfolio_eval",
                  worktree="/tmp/worktrees/portfolio_eval/42"),
    ]
    r = client.get("/api/task/agent_ops/42", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["card"]["target"] == "agent_ops"
    r2 = client.get("/api/task/portfolio_eval/42", headers=HEADERS)
    assert r2.status_code == 200
    assert r2.json()["worktree"] != r.json()["worktree"]


def test_task_detail_unknown_target_is_404(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    assert client.get("/api/task/nonexistent/7",
                      headers=HEADERS).status_code == 404


def test_task_detail_reports_a_starved_wake(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, park=PARK_HUMAN)]
    fake.blocked_wakes.add(("alpha", 7))
    body = client.get("/api/task/alpha/7", headers=HEADERS).json()
    assert body["card"]["wake_blocked"] is True
    assert body["delivery_contract"] == (
        "will deliver when the session resumes — waiting for a free slot")


def test_wake_blocked_by_model_exposes_reason_and_alternatives(tmp_path):
    from tests.webfakes import tracks_policy
    fake = FakeSources()
    cfg = replace(make_config(tmp_path), models=tracks_policy(
        spec=["claude-fable-5-1@high", "claude-opus-5"]))
    client = TestClient(create_app(cfg, fake))
    fake.tasks_list = [make_task(issue=7, stage=Stage.AWAITING_SPEC_REVIEW, park=PARK_WAKE,
                                 picks={"spec": "anthropic/claude-fable-5-1@high"})]
    from tests.usagefakes import session_usage
    fake.usages = {"anthropic": session_usage(0.2, fable=0.9)}

    body = client.get("/api/task/alpha/7", headers=HEADERS).json()
    admission = body["card"]["admission"]
    assert admission["requested"]["model"] == "anthropic/claude-fable-5-1"
    assert admission["requested"]["admitted"] is False
    assert "week·Fable" in admission["requested"]["note"]
    assert [(x["model"], x["admitted"]) for x in admission["alternatives"]] == [
        ("anthropic/claude-opus-5", True)]
    assert body["card"]["model"] == "anthropic/claude-fable-5-1"
    assert body["card"]["track"] == "standard"
    assert body["track_when"] == "Everything."
    board = client.get("/api/board", headers=HEADERS).json()
    card = next(c for column in board["columns"] for c in column["cards"])
    assert card["admission"] == admission


def test_card_model_is_the_first_admitted_entry_when_no_pick_yet(tmp_path):
    from tests.webfakes import tracks_policy
    from tests.usagefakes import session_usage
    fake = FakeSources()
    cfg = replace(make_config(tmp_path), models=tracks_policy(
        plan=["claude-fable-5-1", "claude-opus-5"]))
    client = TestClient(create_app(cfg, fake))
    fake.tasks_list = [make_task(issue=7, stage=Stage.PLAN)]
    fake.usages = {"anthropic": session_usage(0.2, fable=0.9)}
    body = client.get("/api/task/alpha/7", headers=HEADERS).json()
    assert body["card"]["model"] == "anthropic/claude-opus-5"
    assert body["card"]["admission"] is None


def test_task_admission_is_none_when_track_is_unconfigured(tmp_path):
    """A task carrying a track the policy doesn't know (e.g. the legacy
    track="" default, or a renamed/removed track) has no candidates to
    launch on — _model_for is "". That must never be fed to the usage gate
    as a bare model id (it defaults to anthropic and fabricates a false
    "blocked on capacity" verdict); it must show no admission at all."""
    from tests.webfakes import tracks_policy
    from tests.usagefakes import session_usage
    fake = FakeSources()
    cfg = replace(make_config(tmp_path), models=tracks_policy())
    client = TestClient(create_app(cfg, fake))
    fake.tasks_list = [make_task(issue=7, stage=Stage.AWAITING_SPEC_REVIEW,
                                 park=PARK_WAKE, track="")]
    fake.usages = {"anthropic": session_usage(0.99)}
    body = client.get("/api/task/alpha/7", headers=HEADERS).json()
    assert body["card"]["model"] == ""
    assert body["card"]["admission"] is None
    board = client.get("/api/board", headers=HEADERS).json()
    card = next(c for column in board["columns"] for c in column["cards"])
    assert card["admission"] is None


def test_usage_base_allowance(tmp_path):
    from tests.usagefakes import session_usage
    fake, client = rig(tmp_path)
    fake.usages = {"anthropic": session_usage(0.5)}
    body = client.get("/api/usage", headers=HEADERS).json()
    (provider,) = body["providers"]
    assert provider["provider"] == "anthropic"
    assert provider["source"] == "oauth"
    assert provider["windows"][0]["allowance"] == pytest.approx(0.8, abs=1e-3)
    assert set(provider) == {"provider", "source", "windows"}
    gate = body["gate"]
    assert (gate["model"], gate["provider"], gate["admitted"]) == (
        "anthropic/claude-opus-5", "anthropic", True)
    assert gate["binding"]["kind"] == "session"


def test_usage_reset_racing(tmp_path):
    from tests.usagefakes import session_usage
    fake, client = rig(tmp_path)
    # session window resets in 10 min (≤ racing_minutes=30) → racing threshold 0.95
    fake.usages = {"anthropic": session_usage(0.9, mins=10.0)}
    body = client.get("/api/usage", headers=HEADERS).json()
    assert body["gate"]["admitted"] is True   # 0.9 < racing_threshold 0.95
    assert body["providers"][0]["windows"][0]["allowance"] == pytest.approx(0.95, abs=1e-3)


def test_usage_unavailable(tmp_path):
    from tests.usagefakes import session_usage
    fake, client = rig(tmp_path)
    fake.usages = {"anthropic": session_usage(source="unavailable")}
    body = client.get("/api/usage", headers=HEADERS).json()
    (provider,) = body["providers"]
    assert provider["provider"] == "anthropic"
    assert provider["source"] == "unavailable"
    assert provider["windows"] == []
    assert body["gate"]["admitted"] is False
    assert body["gate"]["note"] == "anthropic: usage unavailable"
    assert (body["gate"]["binding"], body["gate"]["minutes_to_reset"]) == (None, 0)


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



def test_task_history_returns_pane_history(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    fake.pane_histories[7] = "old line\nnewer line"
    body = client.get("/api/task/alpha/7/history", headers=HEADERS).json()
    assert body == {"text": "old line\nnewer line"}
    assert fake.history_calls == [(7, 2000)]  # default lines


def test_task_history_honours_lines_and_clamps(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    client.get("/api/task/alpha/7/history?lines=500", headers=HEADERS)
    client.get("/api/task/alpha/7/history?lines=99999", headers=HEADERS)
    assert fake.history_calls == [(7, 500), (7, 10000)]  # clamped to max


def test_task_history_404_for_unknown_task(tmp_path):
    fake, client = rig(tmp_path)
    assert client.get("/api/task/alpha/999/history",
                      headers=HEADERS).status_code == 404


def test_board_carries_next_claim_ghosts_and_timeline(tmp_path):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, stage=Stage.IMPLEMENT)]
    # Heartbeat derived from real now so it is always within the staleness
    # window (now - finished_at < 2 * 10 * 60 = 1200 s).
    fake.heartbeat = _fresh_heartbeat()
    fake.rank["alpha"] = ([{"number": 73, "title": "t73", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 2.0, "boost": 0}],
                          "2026-08-01T12:00:00+00:00", False)
    # Event timestamp well in the past so stage_timeline produces an ongoing
    # segment (now - claimed_at >> 1 s) regardless of time of day.
    fake.events = [{"ts": "2026-07-31T10:00:00+00:00", "event": "claimed",
                    "target": "alpha", "issue": 7, "stage": "queued",
                    "model": "", "actor": "dispatcher", "detail": ""}]
    body = client.get("/api/board", headers=HEADERS).json()
    assert body["next_claim"]["next_issue"] == 73
    queued = next(c for c in body["columns"] if c["key"] == "queued")
    assert [g["number"] for g in queued["ghosts"]] == [73]
    card = [c for col in body["columns"] for c in col["cards"]][0]
    assert card["claimed_at"] == "2026-07-31T10:00:00+00:00"
    detail = client.get("/api/task/alpha/7", headers=HEADERS).json()
    assert detail["timeline"][0]["label"] == "queued"
    assert detail["timeline"][0]["ongoing"] is True


def test_capacity_blocked_queue_candidate_exposes_force_choices(tmp_path):
    from tests.webfakes import tracks_policy
    fake = FakeSources()
    cfg = replace(make_config(tmp_path), models=tracks_policy(
        spec=["claude-fable-5-1", "claude-opus-5"]))
    fake.rank["alpha"] = ([{
        "number": 73, "title": "t73", "url": "u", "status": "Ready",
        "labels": ["auto"], "blocked": False, "score": 2.0, "boost": 0,
    }], "now", False)
    from tests.usagefakes import session_usage
    fake.usages = {"anthropic": session_usage(0.2, fable=0.9)}
    body = TestClient(create_app(cfg, fake)).get(
        "/api/board", headers=HEADERS).json()
    queued = next(c for c in body["columns"] if c["key"] == "queued")
    admission = queued["ghosts"][0]["admission"]
    assert admission["requested"]["model"] == "anthropic/claude-fable-5-1"
    assert admission["requested"]["admitted"] is False
    assert [(x["model"], x["admitted"])
            for x in admission["alternatives"]] == [
                ("anthropic/claude-opus-5", True)]


def test_board_next_claim_claims_paused(tmp_path):
    fake, client = rig(tmp_path)
    # Fresh heartbeat so next_claim does not short-circuit to "unknown".
    fake.heartbeat = _fresh_heartbeat()
    fake.rank["alpha"] = ([{"number": 73, "title": "t73", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 2.0, "boost": 0}],
                          "2026-08-01T12:00:00+00:00", False)
    fake._claims_paused = True
    body = client.get("/api/board", headers=HEADERS).json()
    assert body["next_claim"]["verdict"] == "claims-paused"


def test_board_next_claim_triage_running(tmp_path):
    """capacity=1 + triage_running → effective capacity=0 → capacity-full."""
    fake = FakeSources()
    fake.heartbeat = _fresh_heartbeat()
    fake.rank["alpha"] = ([{"number": 73, "title": "t73", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 2.0, "boost": 0}],
                          "2026-08-01T12:00:00+00:00", False)
    fake._triage_running = True
    client = TestClient(create_app(make_config(tmp_path, capacity=1), fake))
    body = client.get("/api/board", headers=HEADERS).json()
    assert body["next_claim"]["verdict"] == "capacity-full"


def test_board_probes_tmux_at_most_once_per_request(tmp_path):
    """The board asked sources for claims_paused and triage_running
    separately, and triage.pending() calls running() itself — two tmux
    subprocesses (timeout=30 each) per request on the hottest route. Real
    Sources here so the deduplication in sources.py is what is measured."""
    from dispatcher import triage as triage_mod
    from web.sources import Sources
    cfg = make_config(tmp_path)
    calls = []

    class _NullGitHub:
        def rank_rows(self, _): return []

    class _NullSessions:
        def is_alive(self, _): return False

    def _running():
        calls.append(1)
        return False

    import pytest
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(triage_mod, "running", _running)
        client2 = TestClient(create_app(cfg, Sources(cfg, _NullSessions(),
                                                     _NullGitHub())))
        assert client2.get("/api/board", headers=HEADERS).status_code == 200
    assert len(calls) == 1, f"tmux probed {len(calls)} times per board request"


def test_task_detail_200_with_mixed_timezone_events(tmp_path):
    """A naive `ts` beside an aware `now` raised TypeError out of
    stage_timeline and 500'd this route; it now degrades by dropping the
    incomparable segment."""
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    fake.events = [
        {"ts": "2026-07-31T10:00:00+00:00", "event": "claimed",
         "target": "alpha", "issue": 7, "stage": "queued", "model": "",
         "actor": "dispatcher", "detail": ""},
        {"ts": "2026-07-31T11:00:00", "event": "stage-started",  # NAIVE
         "target": "alpha", "issue": 7, "stage": "spec", "model": "",
         "actor": "dispatcher", "detail": ""},
    ]
    resp = client.get("/api/task/alpha/7", headers=HEADERS)
    assert resp.status_code == 200
    assert isinstance(resp.json()["timeline"], list)


def test_board_degrades_to_200_on_corrupt_events(tmp_path):
    """An unreadable events.jsonl must not 500 /api/board; the board still
    renders with empty event-derived fields (no claimed_at, no timeline).
    Uses real Sources so the events_tail degrade in sources.py is exercised."""
    from dispatcher import state as state_mod
    from tests.webfakes import make_task as _make_task
    from web.sources import Sources
    cfg = make_config(tmp_path)
    # Write a task so the board has something to show.
    state_mod.save(tmp_path, _make_task(issue=7))
    # Write a corrupt events file that will raise UnicodeDecodeError.
    (tmp_path / "events.jsonl").write_bytes(b"\xff\xfe bad utf-8")

    class _NullGitHub:
        def rank_rows(self, _): return []

    class _NullSessions:
        def is_alive(self, _): return False

    src = Sources(cfg, _NullSessions(), _NullGitHub())
    client2 = TestClient(create_app(cfg, src))
    resp = client2.get("/api/board", headers=HEADERS)
    assert resp.status_code == 200
    card = [c for col in resp.json()["columns"] for c in col["cards"]][0]
    assert card["claimed_at"] == ""


def test_description_for_task_and_ghost(tmp_path):
    """With `target` in the path there is no cross-target ambiguity to
    resolve: a claimed issue and a ghost (not yet claimed, still on the
    target's queue) both resolve straight to targets_by_name[target].repo.
    An issue that is neither a task nor on this target's queue is still a
    true 404 — target-scoping removed the old ambiguity check, not the
    existence check."""
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    fake.rank["alpha"] = ([{"number": 73, "title": "t", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 1.0, "boost": 0}],
                          "2026-08-01T12:00:00+00:00", False)
    fake.descriptions[("jesdi/alpha", 7)] = {
        "title": "T7", "body": "B", "url": "u7",
        "fetched_at": "2026-08-01T12:00:00+00:00", "error": ""}
    fake.descriptions[("jesdi/alpha", 73)] = {
        "title": "T73", "body": "B", "url": "u73",
        "fetched_at": "2026-08-01T12:00:00+00:00", "error": ""}
    assert client.get("/api/task/alpha/7/description",
                      headers=HEADERS).json()["title"] == "T7"
    assert client.get("/api/task/alpha/73/description",
                      headers=HEADERS).json()["title"] == "T73"
    assert client.get("/api/task/alpha/999/description",
                      headers=HEADERS).status_code == 404


def test_description_404_on_unknown_target(tmp_path):
    """The old issue-only route had to scan every target's queue for a hit
    and could find the number on two of them at once — hence the old 409
    ambiguity contract. With `target` in the path that scan (and the
    ambiguity it could hit) is gone; an unrecognised target name is still a
    404, same as any target-scoped route."""
    fake, client = rig(tmp_path)
    resp = client.get("/api/task/nonexistent-target/73/description",
                      headers=HEADERS)
    assert resp.status_code == 404


def test_description_resolves_each_targets_own_repo(tmp_path):
    """Two targets can both hold issue #73 as a claimed task (or a ghost) —
    the old issue-only route could only guess (or 409) which repo's body to
    serve. With `target` in the path each call must resolve strictly to
    THAT target's repo, never the other one's, even for the same number."""
    from tests.webfakes import make_target
    fake = FakeSources()
    cfg = make_config(tmp_path, targets=[make_target("alpha", "jesdi/alpha"),
                                         make_target("beta", "jesdi/beta")])
    client = TestClient(create_app(cfg, fake))
    r73 = [{"number": 73, "title": "t", "url": "u", "status": "Ready",
            "labels": ["auto"], "blocked": False, "score": 1.0, "boost": 0}]
    fake.rank["alpha"] = (r73, "2026-08-01T12:00:00+00:00", False)
    fake.rank["beta"] = (r73, "2026-08-01T12:00:00+00:00", False)
    fake.descriptions[("jesdi/alpha", 73)] = {
        "title": "alpha-73", "body": "", "url": "",
        "fetched_at": "2026-08-01T12:00:00+00:00", "error": ""}
    fake.descriptions[("jesdi/beta", 73)] = {
        "title": "beta-73", "body": "", "url": "",
        "fetched_at": "2026-08-01T12:00:00+00:00", "error": ""}
    a = client.get("/api/task/alpha/73/description", headers=HEADERS)
    b = client.get("/api/task/beta/73/description", headers=HEADERS)
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["title"] == "alpha-73"
    assert b.json()["title"] == "beta-73"


def test_failures_degrades_to_200_on_corrupt_quarantine_record(tmp_path):
    """A torn quarantine record (invalid UTF-8 -> UnicodeDecodeError, a
    ValueError not an OSError) must not 500 /api/failures. Real Sources so
    the guard in sources.py is what is exercised, mirroring the corrupt-
    events test above. The unreadable records are SKIPPED, not surfaced: a
    row with every field blank is indistinguishable from a real entry, and
    the console's rule is never a blank, always an explicit state."""
    from web.sources import Sources
    cfg = make_config(tmp_path)
    (tmp_path / "quarantine").mkdir(parents=True, exist_ok=True)
    (tmp_path / "quarantine" / "alpha-73.json").write_bytes(b"\xff\xfe torn")
    (tmp_path / "quarantine" / "alpha-74.json").write_text("null")
    (tmp_path / "quarantine" / "alpha-75.json").write_text(json.dumps(
        {"task_issue": 75, "blocker_repo": "r", "blocker_issue": 0,
         "fingerprint": "f", "created_at": "c"}))

    class _NullGitHub:
        def rank_rows(self, _): return []

    class _NullSessions:
        def is_alive(self, _): return False

    client2 = TestClient(create_app(cfg, Sources(cfg, _NullSessions(),
                                                 _NullGitHub())))
    resp = client2.get("/api/failures", headers=HEADERS)
    assert resp.status_code == 200
    assert [q["task_issue"] for q in resp.json()["quarantined"]] == [75]


def test_terminal_websocket_route_is_gone(tmp_path):
    """The board's PTY bridge was removed with the herdr migration; attach
    is `herdr --remote box` from a terminal. The path must 404 (a plain
    HTTP GET on a former websocket route), never upgrade."""
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7)]
    r = client.get("/api/task/alpha/7/terminal", headers=HEADERS)
    assert r.status_code == 404




def test_board_snapshot_never_waits_for_live_sources(tmp_path, monkeypatch):
    fake, client = rig(tmp_path)
    fake.tasks_list = [make_task(issue=7, stage=Stage.IMPLEMENT),
                       make_task(issue=8, stage=Stage.SPEC, park=PARK_HUMAN)]
    full = client.get("/api/board", headers=HEADERS).json()

    def unavailable(*args, **kwargs):
        raise AssertionError("snapshot must not call live sources")

    for name in ("rank_rows", "triage_state", "usage"):
        monkeypatch.setattr(fake, name, unavailable)
    response = client.get("/api/board/snapshot", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {key: full[key] for key in (
        "columns", "capacity", "median_cycle_seconds")}
    assert client.get("/api/board/snapshot").status_code == 401
