"""Sources against tmp_path state dirs and hand-written github/session fakes."""
import json
import pytest
import subprocess
from datetime import datetime, timezone

from dispatcher import eventlog, execution_overrides, state
from dispatcher.queue_ops import QueuePlan
from tests.webfakes import make_config, make_repository
from web.sources import (DESCRIPTION_RETRY_SECONDS, DESCRIPTION_TTL_SECONDS,
                         RANK_TTL_SECONDS, Sources)


ISSUE = {"title": "T", "body": "B", "url": "u"}


class FakeGitHub:
    """rank_rows and issue_view share the failure knobs: `fail` (True or the
    exception to raise) and `on_call`, run at the start of each call to model
    a slow gh (advance the clock) or an overlapping request (re-enter)."""
    def __init__(self):
        self.rows = {}          # repository name -> list[dict]
        self.calls = 0          # rank_rows calls
        self.issues = {}        # (repo, number) -> issue_view payload
        self.view_calls = 0     # issue_view calls
        self.fail = False
        self.on_call = None
        self.states = {}        # (repo, number) -> "OPEN"/"CLOSED"/None
        self.state_calls = []   # (repo, number) per issue_state call
        self.boosts = []

    def _probe(self, *args):
        if self.on_call:
            self.on_call(*args)
        if self.fail:
            raise (RuntimeError("gh exploded") if self.fail is True
                   else self.fail)

    def rank_rows(self, repositories):
        self.calls += 1
        self._probe(repositories)
        return self.rows.get(repositories[0].name, [])

    def issue_view(self, repo, number):
        self.view_calls += 1
        self._probe(repo, number)
        return self.issues.get((repo, number), ISSUE)

    def issue_state(self, repo, number):
        self.state_calls.append((repo, number))
        v = self.states.get((repo, number))
        if v is None:
            raise RuntimeError("gh failed")
        return v

    def set_boost(self, repository, issue, value):
        self.boosts.append((repository.name, issue, value))

    def set_status(self, repository, issue, option_id):
        pass

    def add_label(self, repository, issue, label):
        pass


class FakeSessions:
    def __init__(self):
        self.tails = {}
        self.histories = {}
        self.alive = set()

    def capture_tail(self, repository, issue, lines=25):
        return self.tails.get(issue, "")

    def capture_history(self, repository, issue, lines=2000):
        return self.histories.get(issue, "")

    def is_alive(self, repository, issue):
        return issue in self.alive


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_sources(tmp_path, github=None, sessions=None, clock=None,
                 systemctl=("true",)):
    cfg = make_config(tmp_path)
    return cfg, Sources(cfg.state_dir, sessions or FakeSessions(),
                        github or FakeGitHub(), clock=clock or FakeClock(),
                        systemctl=systemctl)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def src(tmp_path, gh, clock):
    return make_sources(tmp_path, github=gh, clock=clock)[1]


@pytest.fixture
def repository():
    return make_repository()


def test_tasks_reads_state_dir(tmp_path, src):
    from tests.webfakes import make_task
    state.save(tmp_path, make_task(issue=5))
    assert [t.issue for t in src.tasks()] == [5]


def test_execution_override_is_durable_and_invalidates_board(src):
    before = json.loads(src.state_fingerprint())["board"]

    src.set_execution_override(
        "alpha", 9, model="claude-fable-5", bypass_usage=True)

    assert src.execution_override("alpha", 9) == (
        execution_overrides.ExecutionOverride(
            model="claude-fable-5", bypass_usage=True))
    assert json.loads(src.state_fingerprint())["board"] != before


def test_usage_reads_fresh_cache_only(tmp_path):
    from dispatcher.usage_providers import usage_to_json
    from tests.usagefakes import session_usage
    clock = FakeClock()
    (tmp_path / "usage").mkdir(parents=True, exist_ok=True)
    (tmp_path / "usage" / "anthropic.json").write_text(json.dumps({
        "fetched_at": clock.t,
        "usage": usage_to_json(session_usage(0.42))}))
    _, src = make_sources(tmp_path, clock=clock)
    usages = src.usage({"anthropic"})
    assert usages["anthropic"].windows[0].used == pytest.approx(0.42, abs=1e-3)



def _usage_route(tmp_path, monkeypatch, openai_reply):
    """GET /api/usage through the real Sources and the real OpenAI adapter;
    openai_reply stands in for wham/usage (a dict, or an exception to raise).
    No track routes to openai, and Anthropic reads a fixed session."""
    from fastapi.testclient import TestClient
    from dispatcher import usage_providers
    from tests.usagefakes import session_usage
    from tests.webfakes import HEADERS
    from web.app import create_app

    class Anthropic:
        def fetch(self, state_dir, *, now):
            return session_usage(0.42)

    def http(url, headers):
        if isinstance(openai_reply, Exception):
            raise openai_reply
        return openai_reply

    monkeypatch.setitem(usage_providers.ADAPTERS, "anthropic", Anthropic())
    monkeypatch.setattr(usage_providers, "_http_get_json", http)
    cfg, src = make_sources(tmp_path)
    with TestClient(create_app(cfg, src)) as client:
        body = client.get("/api/usage", headers=HEADERS).json()
    return {p["provider"]: p for p in body["providers"]}


def _codex_login(tmp_path):
    (tmp_path / "codex-home").mkdir()
    (tmp_path / "codex-home" / "auth.json").write_text(json.dumps(
        {"tokens": {"access_token": "a", "account_id": "b"}}))


def test_console_shows_codex_usage_while_logged_in_though_no_track_routes_to_it(
        tmp_path, monkeypatch):
    from pathlib import Path
    reply = json.loads((Path(__file__).parent / "fixtures" / "openai-usage.json").read_text())
    _codex_login(tmp_path)
    providers = _usage_route(tmp_path, monkeypatch, reply)
    assert set(providers) == {"anthropic", "openai"}
    assert providers["openai"]["source"] == "oauth"
    assert [w["kind"] for w in providers["openai"]["windows"]] == ["weekly"]


def test_console_hides_codex_without_a_readable_login(tmp_path, monkeypatch):
    from dispatcher.usage_providers import UsageFetchError
    assert set(_usage_route(tmp_path, monkeypatch, {})) == {"anthropic"}
    _codex_login(tmp_path)
    assert set(_usage_route(tmp_path, monkeypatch, UsageFetchError("401"))) == {"anthropic"}


def test_failure_and_quarantine_entries(tmp_path, src):
    (tmp_path / "failures").mkdir()
    (tmp_path / "failures" / "abc123def456").write_text(json.dumps(
        {"repo": "jesdi/alpha", "issue": 4, "when": "2026-07-25T09:00:00"}))
    (tmp_path / "quarantine").mkdir()
    (tmp_path / "quarantine" / "alpha-7.json").write_text(json.dumps(
        {"task_issue": 7, "blocker_repo": "jesdi/agent-ops",
         "blocker_issue": 31, "fingerprint": "abc123def456",
         "created_at": "2026-07-25T09:05:00"}))
    fps = src.fingerprint_entries()
    assert fps == [{"fingerprint": "abc123def456", "repo": "jesdi/alpha",
                    "issue": 4, "when": "2026-07-25T09:00:00"}]
    qs = src.quarantine_entries()
    assert qs[0]["repository"] == "alpha" and qs[0]["task_issue"] == 7


def test_issue_open_maps_states_and_failures(tmp_path):
    gh = FakeGitHub()
    gh.states[("r", 1)] = "OPEN"
    gh.states[("r", 2)] = "CLOSED"
    _, src = make_sources(tmp_path, github=gh)
    assert src.issue_open("r", 1) is True
    assert src.issue_open("r", 2) is False
    assert src.issue_open("r", 3) is None  # gh raised


def test_events_tail_via_real_eventlog(tmp_path, src):
    eventlog.append_event(tmp_path, "claimed", repository="alpha", issue=7,
                          stage="spec", actor="dispatcher")
    tail = src.events_tail(10)
    assert tail[-1]["event"] == "claimed" and tail[-1]["issue"] == 7


def test_events_tail_degrades_to_empty_on_corrupt_log(tmp_path, src):
    """A torn or undecodable events.jsonl must not 500 /api/board or
    /api/task/{issue}; degrade to [] so the board still renders."""
    (tmp_path / "events.jsonl").write_bytes(b"\xff\xfe bad utf-8")
    assert src.events_tail(100) == []


def test_submit_intent_writes_file_and_kicks_dispatcher(tmp_path):
    kick_log = tmp_path / "kick.log"
    _, src = make_sources(
        tmp_path,
        systemctl=("sh", "-c", f"echo kicked >> {kick_log}"))
    name = src.submit_intent("reply", "alpha", 7, {"text": "hi"}, "jesdi@github")
    assert name.endswith("-7-reply.json")
    assert (tmp_path / "intents" / name).exists()
    assert kick_log.read_text().strip() == "kicked"
    pend = src.pending_intents()
    assert pend[0]["action"] == "reply" and pend[0]["issue"] == 7
    assert pend[0]["actor"] == "jesdi@github" and pend[0]["created_at"]


def test_submit_intent_survives_kick_failure(tmp_path):
    _, src = make_sources(tmp_path,
                          systemctl=("/nonexistent-binary-xyz",))
    name = src.submit_intent("park", "alpha", 7, {}, "jesdi@github")
    assert (tmp_path / "intents" / name).exists()


def test_apply_queue_plan_delegates_to_queue_ops(tmp_path):
    gh = FakeGitHub()
    _, src = make_sources(tmp_path, github=gh)
    src.apply_queue_plan(make_repository(), 9,
                         QueuePlan(ok=True, set_boost=3))
    assert gh.boosts == [("alpha", 9, 3)]


def test_state_fingerprint_tracks_categories(tmp_path, src):
    from tests.webfakes import make_task
    f1 = json.loads(src.state_fingerprint())
    state.save(tmp_path, make_task(issue=5))
    f2 = json.loads(src.state_fingerprint())
    assert f2["board"] != f1["board"]
    assert f2["usage"] == f1["usage"]
    eventlog.append_event(tmp_path, "claimed", issue=5)
    f3 = json.loads(src.state_fingerprint())
    assert f3["history"] != f2["history"]


def test_fingerprint_ignores_attached_markers(tmp_path, src):
    """attached-* is no longer state the console reads: a stale marker from
    a pre-deploy console must not keep the board SSE key churning."""
    f1 = json.loads(src.state_fingerprint())
    (tmp_path / "attached-alpha-7").write_text("")
    assert json.loads(src.state_fingerprint())["board"] == f1["board"]


def test_quarantine_and_intent_entries_tolerate_oserror(tmp_path, src):
    """If a file is deleted or becomes unreadable after glob() but before
    read_text() (race condition during dispatcher processing), OSError
    should be caught silently. We simulate this by creating a directory
    with .json extension."""

    # Add a valid quarantine entry
    (tmp_path / "quarantine").mkdir()
    (tmp_path / "quarantine" / "valid.json").write_text(json.dumps(
        {"task_issue": 1, "blocker_issue": 2}))

    # Add a "file" that's actually a directory (will raise IsADirectoryError
    # when read_text() is called, which is a subclass of OSError)
    (tmp_path / "quarantine" / "broken.json").mkdir()

    # Should skip the broken directory but include the valid entry
    qs = src.quarantine_entries()
    assert len(qs) == 1
    assert qs[0]["task_issue"] == 1

    # Same for intents
    (tmp_path / "intents").mkdir()
    (tmp_path / "intents" / "valid.json").write_text(json.dumps(
        {"action": "reply", "issue": 5}))
    (tmp_path / "intents" / "broken.json").mkdir()

    pend = src.pending_intents()
    assert len(pend) == 1
    assert pend[0]["action"] == "reply"


class WedgedSessions(FakeSessions):
    """tmux server that never answers: capture_tail RAISES TimeoutExpired."""

    def capture_tail(self, repository, issue, lines=25):
        raise subprocess.TimeoutExpired(["tmux", "capture-pane"], 30)


def test_pane_tail_degrades_to_empty_when_tmux_is_wedged(tmp_path):
    """sessions.capture_tail uses subprocess.run(timeout=30), which raises.
    Unhandled it turns GET /api/task/{issue} into a 500 after 30 s."""
    sess = WedgedSessions()
    sess.alive.add(7)
    _, src = make_sources(tmp_path, sessions=sess)
    assert src.pane_tail("alpha", 7) == ""


def test_pane_history_delegates_to_capture_history(tmp_path):
    sessions = FakeSessions()
    sessions.alive.add(7)
    sessions.histories = {7: "old\nnew"}
    _, src = make_sources(tmp_path, sessions=sessions)
    assert src.pane_history("alpha", 7) == "old\nnew"


def test_pane_history_degrades_to_empty_on_tmux_error(tmp_path):
    class BoomSessions(FakeSessions):
        def capture_history(self, repository, issue, lines=2000):
            raise TimeoutError("tmux wedged")
    sess = BoomSessions()
    sess.alive.add(7)
    _, src = make_sources(tmp_path, sessions=sess)
    assert src.pane_history("alpha", 7) == ""


def _write_snapshot(tmp_path, issue, text):
    snap = tmp_path / "snapshots" / f"task-{issue}.txt"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(text)
    return snap


def test_pane_tail_falls_back_to_snapshot_when_dead(tmp_path):
    _write_snapshot(tmp_path, 7, "old output\nlast line")
    _, src = make_sources(tmp_path)  # 7 not in alive set
    assert src.pane_tail("alpha", 7) == "old output\nlast line"


def test_pane_tail_prefers_live_capture(tmp_path):
    _write_snapshot(tmp_path, 7, "stale snapshot")
    sess = FakeSessions()
    sess.alive.add(7)
    sess.tails[7] = "live output"
    _, src = make_sources(tmp_path, sessions=sess)
    assert src.pane_tail("alpha", 7) == "live output"


def test_pane_tail_dead_and_no_snapshot_degrades_to_empty(src):
    assert src.pane_tail("alpha", 7) == ""


def test_pane_history_falls_back_to_snapshot_when_dead(tmp_path, src):
    _write_snapshot(tmp_path, 7, "l1\nl2\nl3")
    assert src.pane_history("alpha", 7) == "l1\nl2\nl3"


def test_pane_tail_wedged_is_alive_degrades_to_empty(tmp_path):
    class WedgedSessions(FakeSessions):
        def is_alive(self, repository, issue):
            raise subprocess.TimeoutExpired(cmd="tmux", timeout=30)

    _write_snapshot(tmp_path, 7, "snapshot")
    _, src = make_sources(tmp_path, sessions=WedgedSessions())
    assert src.pane_tail("alpha", 7) == ""


def test_pane_tail_corrupt_snapshot_degrades_to_empty(tmp_path, src):
    """Snapshot truncated mid-UTF-8 (e.g. by dispatcher kill) raises
    UnicodeDecodeError; must degrade to "" not 500 the task view."""
    snap = tmp_path / "snapshots" / "task-7.txt"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(b"\xff\xfe\xff")  # Invalid UTF-8 sequence
    assert src.pane_tail("alpha", 7) == ""


def test_pane_history_corrupt_snapshot_degrades_to_empty(tmp_path, src):
    """Same as pane_tail: corrupt snapshot with invalid UTF-8 degrades."""
    snap = tmp_path / "snapshots" / "task-7.txt"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(b"\x80\x81\x82")  # Invalid UTF-8 sequence
    assert src.pane_history("alpha", 7) == ""


def test_pass_heartbeat_reads_and_degrades(src):
    assert src.pass_heartbeat() is None
    src.state_dir.mkdir(parents=True, exist_ok=True)
    (src.state_dir / "pass.json").write_text('{"interval_minutes": 10}')
    assert src.pass_heartbeat() == {"interval_minutes": 10}
    (src.state_dir / "pass.json").write_text("not json")
    assert src.pass_heartbeat() is None


def test_state_fingerprint_changes_when_pass_json_changes(tmp_path, src):
    f1 = json.loads(src.state_fingerprint())
    (tmp_path / "pass.json").write_text('{"interval_minutes": 10}')
    f2 = json.loads(src.state_fingerprint())
    assert f2["board"] != f1["board"]


def test_state_fingerprint_usage_key_follows_the_usage_cache(tmp_path, src):
    """A fresh usage/<provider>.json must reach open consoles over SSE."""
    f1 = json.loads(src.state_fingerprint())
    (tmp_path / "usage").mkdir()
    (tmp_path / "usage" / "anthropic.json").write_text('{"fetched_at": 1}')
    f2 = json.loads(src.state_fingerprint())
    assert f2["usage"] != f1["usage"]
    assert f2["board"] == f1["board"]


def test_triage_state_degrades_to_false(monkeypatch, src):
    """Both triage flags degrade to False when the underlying call raises.
    Monkeypatching forces the error path so the test does not depend on
    whether the host has a tmux session named 'triage' or tmux installed."""
    import dispatcher.triage as triage_mod

    def boom(*_a, **_kw):
        raise RuntimeError("simulated triage read failure")

    monkeypatch.setattr(triage_mod, "pending", boom)
    monkeypatch.setattr(triage_mod, "running", boom)

    assert src.triage_state() == (False, False)


def test_triage_state_probes_tmux_once(monkeypatch, src):
    """triage.pending() calls running() internally; composing the two signals
    from one probe is the whole point (cf. dispatcher run_pass)."""
    import dispatcher.triage as triage_mod
    calls = []
    monkeypatch.setattr(triage_mod, "running",
                        lambda: (calls.append(1), False)[1])
    assert src.triage_state() == (False, False)
    assert len(calls) == 1


def test_triage_state_request_file_pauses_claims(tmp_path, monkeypatch, src):
    import dispatcher.triage as triage_mod
    monkeypatch.setattr(triage_mod, "running", lambda: False)
    (tmp_path / triage_mod.REQUEST_FILE).write_text(
        '{"requested_at": "2026-08-01T00:00:00+00:00"}')
    assert src.triage_state() == (True, False)


def test_triage_state_running_pauses_claims_too(monkeypatch, src):
    import dispatcher.triage as triage_mod
    monkeypatch.setattr(triage_mod, "running", lambda: True)
    assert src.triage_state() == (True, True)


# --- triage probe cache -----------------------------------------------------

def test_triage_state_unreadable_request_file_keeps_the_probed_value(tmp_path, monkeypatch):
    """A request-file read error must not hide a running probe nor invent a pause."""
    import dispatcher.triage as triage_mod

    def boom(*_a, **_kw):
        raise OSError("simulated unreadable request file")

    monkeypatch.setattr(triage_mod, "load_request", boom)
    for probed in (True, False):
        monkeypatch.setattr(triage_mod, "running", lambda probed=probed: probed)
        _, src = make_sources(tmp_path)
        assert src.triage_state() == (probed, probed)


def _triage_sources(tmp_path, monkeypatch, probe):
    """Sources + clock + call counter around a monkeypatched triage.running."""
    import dispatcher.triage as triage_mod
    import web.sources as sources_mod
    calls = []

    def running():
        calls.append(1)
        return probe(len(calls))

    monkeypatch.setattr(triage_mod, "running", running)
    clock = FakeClock()
    _, src = make_sources(tmp_path, clock=clock)
    ttl = getattr(sources_mod, "TRIAGE_TTL_SECONDS", 5.0)
    return src, clock, calls, ttl


def test_triage_state_two_calls_within_ttl_probe_once(tmp_path, monkeypatch):
    src, clock, calls, _ = _triage_sources(
        tmp_path, monkeypatch, lambda n: False)
    assert src.triage_state() == (False, False)
    clock.t += 2
    assert src.triage_state() == (False, False)
    assert len(calls) == 1


def test_triage_state_reprobes_after_ttl_and_reports_new_value(
        tmp_path, monkeypatch):
    src, clock, calls, ttl = _triage_sources(
        tmp_path, monkeypatch, lambda n: n > 1)
    assert src.triage_state() == (False, False)
    clock.t += 2
    assert src.triage_state() == (False, False)
    clock.t += ttl + 1
    assert src.triage_state() == (True, True)
    assert len(calls) == 2


def test_triage_state_reprobes_exactly_ttl_after_the_probe(
        tmp_path, monkeypatch):
    src, clock, calls, ttl = _triage_sources(
        tmp_path, monkeypatch, lambda n: False)
    src.triage_state()
    clock.t += ttl - 0.1
    src.triage_state()
    assert len(calls) == 1
    clock.t += 0.1  # now exactly ttl after the probe: strict < means re-probe
    src.triage_state()
    assert len(calls) == 2


def test_triage_state_just_inside_ttl_reuses_the_cache(tmp_path, monkeypatch):
    src, clock, calls, ttl = _triage_sources(
        tmp_path, monkeypatch, lambda n: False)
    src.triage_state()
    clock.t += ttl - 0.1
    src.triage_state()
    assert len(calls) == 1


def test_triage_state_failing_probe_degrades_and_is_cached(
        tmp_path, monkeypatch):
    def boom(n):
        raise RuntimeError("simulated probe failure")

    src, clock, calls, _ = _triage_sources(tmp_path, monkeypatch, boom)
    assert src.triage_state() == (False, False)
    clock.t += 1
    assert src.triage_state() == (False, False)
    assert len(calls) == 1


def test_triage_state_failing_probe_is_retried_after_ttl(
        tmp_path, monkeypatch):
    def flaky(n):
        if n == 1:
            raise RuntimeError("simulated probe failure")
        return True

    src, clock, calls, ttl = _triage_sources(tmp_path, monkeypatch, flaky)
    assert src.triage_state() == (False, False)
    clock.t += 1
    assert src.triage_state() == (False, False)
    assert len(calls) == 1
    clock.t += ttl + 1
    assert src.triage_state() == (True, True)
    assert len(calls) == 2


def test_triage_state_new_request_file_shows_without_a_new_probe(
        tmp_path, monkeypatch):
    import dispatcher.triage as triage_mod
    src, clock, calls, _ = _triage_sources(
        tmp_path, monkeypatch, lambda n: False)
    assert src.triage_state() == (False, False)
    (tmp_path / triage_mod.REQUEST_FILE).write_text(
        '{"requested_at": "2026-08-01T00:00:00+00:00"}')
    clock.t += 1
    assert src.triage_state() == (True, False)
    assert len(calls) == 1


def test_triage_state_removed_request_file_clears_pause_without_a_probe(
        tmp_path, monkeypatch):
    import dispatcher.triage as triage_mod
    (tmp_path / triage_mod.REQUEST_FILE).write_text(
        '{"requested_at": "2026-08-01T00:00:00+00:00"}')
    src, clock, calls, _ = _triage_sources(
        tmp_path, monkeypatch, lambda n: False)
    assert src.triage_state() == (True, False)
    triage_mod.clear_request(tmp_path)
    clock.t += 1
    assert src.triage_state() == (False, False)
    assert len(calls) == 1


def test_triage_state_cached_running_true_still_pauses_claims(
        tmp_path, monkeypatch):
    src, clock, calls, _ = _triage_sources(
        tmp_path, monkeypatch, lambda n: True)
    assert src.triage_state() == (True, True)
    clock.t += 1
    assert src.triage_state() == (True, True)
    assert len(calls) == 1


def test_triage_state_cache_follows_the_injected_clock(tmp_path, monkeypatch):
    import time
    src, clock, calls, ttl = _triage_sources(
        tmp_path, monkeypatch, lambda n: False)
    src.triage_state()
    real_now = time.time()
    monkeypatch.setattr(time, "time", lambda: real_now + 10 * ttl)
    monkeypatch.setattr(time, "monotonic", lambda: real_now + 10 * ttl)
    src.triage_state()
    assert len(calls) == 1
    clock.t += ttl
    src.triage_state()
    assert len(calls) == 2


def _quarantine(tmp_path, name, **rec):
    d = tmp_path / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(rec))


def test_quarantine_entries_skip_an_unreadable_record(tmp_path, src):
    """A torn write_text leaves invalid UTF-8, whose read_text raises
    UnicodeDecodeError — a ValueError, NOT an OSError. The old
    (JSONDecodeError, OSError) guard missed it and 500'd the route. Valid
    JSON that is not an object (null, a list) has the same effect via
    rec.get -> AttributeError. Neither may raise, and neither may reach
    /api/failures: with no readable body every field would be blank and the
    row would be indistinguishable from a real entry."""
    d = tmp_path / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    (d / "alpha-73.json").write_bytes(b'{"blocker_issue": \xff\xfe}')
    (d / "alpha-74.json").write_text("null")
    (d / "alpha-75.json").write_text("[1, 2]")
    _quarantine(tmp_path, "alpha-76.json", task_issue=76, blocker_repo="r",
                blocker_issue=1, fingerprint="f", created_at="c")
    assert [e["task_issue"] for e in src.quarantine_entries()] == [76]


def test_quarantine_key_comes_from_the_filename_not_the_body(tmp_path, src):
    """check_quarantine — and the retry route that validates against these
    entries — looks a record up by FILENAME. A body whose task_issue
    disagrees must not shift the key."""
    _quarantine(tmp_path, "alpha-73.json", task_issue=999, blocker_repo="r",
                blocker_issue=1, fingerprint="f", created_at="c")
    entry, = src.quarantine_entries()
    assert (entry["repository"], entry["task_issue"]) == ("alpha", 73)


def test_state_fingerprint_board_and_usage_follow_the_priority_mode(tmp_path, src):
    """A mode change must reach other open consoles over SSE: it moves the
    mode, the "first" chip and the board's next-launch models."""
    from datetime import datetime, timezone
    from dispatcher import priority
    f1 = json.loads(src.state_fingerprint())
    priority.save(tmp_path, "anthropic", actor="jesdi",
                  now=datetime.now(timezone.utc))
    f2 = json.loads(src.state_fingerprint())
    assert f2["board"] != f1["board"]
    assert f2["usage"] != f1["usage"]



_GOOD = dict(task_issue=0, blocker_repo="r", blocker_issue=1,
             fingerprint="f", created_at="c")


def test_quarantine_entries_skip_wrong_typed_blocker_issue(tmp_path, src):
    for n, bad in enumerate(["abc", "5", True, 1.5, None, [1]], start=80):
        _quarantine(tmp_path, f"alpha-{n}.json",
                    **{**_GOOD, "task_issue": n, "blocker_issue": bad})
    _quarantine(tmp_path, "alpha-86.json",
                **{**_GOOD, "task_issue": 86, "blocker_issue": 7})
    entry, = src.quarantine_entries()
    assert (entry["task_issue"], entry["blocker_issue"]) == (86, 7)


def test_quarantine_entries_skip_wrong_typed_task_issue_despite_filename(tmp_path, src):
    _quarantine(tmp_path, "alpha-90.json", **{**_GOOD, "task_issue": "x"})
    assert [e["task_issue"] for e in src.quarantine_entries()] == []


def test_quarantine_entries_skip_wrong_typed_string_fields(tmp_path, src):
    for n, key, bad in [(91, "blocker_repo", 5), (92, "fingerprint", None),
                        (93, "created_at", {"at": 1})]:
        _quarantine(tmp_path, f"alpha-{n}.json",
                    **{**_GOOD, "task_issue": n, key: bad})
    _quarantine(tmp_path, "alpha-94.json", **{**_GOOD, "task_issue": 94})
    assert [e["task_issue"] for e in src.quarantine_entries()] == [94]


def test_quarantine_entries_keep_missing_fields_and_extra_keys(tmp_path, src):
    _quarantine(tmp_path, "alpha-95.json", blocker_repo="r", note=42)
    entry, = src.quarantine_entries()
    assert (entry["repository"], entry["task_issue"], entry["blocker_repo"],
            entry["blocker_issue"], entry["fingerprint"],
            entry["created_at"]) == ("alpha", 95, "r", 0, "", "")


def test_quarantine_entries_existing_skips_still_hold(tmp_path, src):
    d = tmp_path / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    (d / "alpha-73.json").write_bytes(b"\xff\xfe\xfd")
    (d / "alpha-74.json").write_text("null")
    (d / "alpha-75.json").write_text("[1, 2]")
    _quarantine(tmp_path, "alpha-76.json", **{**_GOOD, "task_issue": 999})
    entry, = src.quarantine_entries()
    assert (entry["repository"], entry["task_issue"]) == ("alpha", 76)


# gh-backed caches: rank_rows and issue_description.


@pytest.fixture
def stale_rank(src, gh, clock, repository):
    """A cached ranking past its TTL, with gh now down."""
    gh.rows["alpha"] = [{"number": 9, "boost": 0}]
    cached = src.rank_rows([repository])
    gh.fail = True
    clock.advance(RANK_TTL_SECONDS + 1)
    return cached


@pytest.fixture
def stale_description(src, gh, clock):
    """A cached description of alpha#73 past its TTL, with gh now down."""
    cached = src.issue_description("jesdi/alpha", 73)
    gh.fail = True
    clock.advance(DESCRIPTION_TTL_SECONDS + 1)
    return cached


def test_rank_rows_cached_within_ttl(src, gh, clock, repository):
    gh.rows["alpha"] = [{"number": 9, "title": "t", "url": "u",
                        "status": "Ready", "labels": ["auto"],
                        "blocked": False, "score": 1.0, "boost": 0}]
    first = src.rank_rows([repository])
    assert src.rank_rows([repository]) == first
    assert first[2] is False and gh.calls == 1
    clock.advance(RANK_TTL_SECONDS + 1)
    src.rank_rows([repository])
    assert gh.calls == 2


def test_rank_failure_serves_stale_cache(src, repository, stale_rank):
    rows, as_of, _ = stale_rank
    assert src.rank_rows([repository]) == (rows, as_of, True)


def test_rank_failure_without_cache_is_empty_stale(src, gh, repository):
    gh.fail = True
    rows, as_of, stale = src.rank_rows([repository])
    assert rows == [] and stale is True and as_of


def test_rank_rows_outage_reprobes_at_ttl_cadence_not_per_request(
        src, gh, clock, repository, stale_rank):
    """#83: during a gh outage every request re-shelled gh. A failed probe,
    stale or cold, must be remembered for exactly one TTL."""
    rows, as_of, _ = stale_rank
    for _ in range(5):
        assert src.rank_rows([repository]) == (rows, as_of, True)
    assert gh.calls == 2   # one probe on expiry, then the stale serve holds
    clock.advance(RANK_TTL_SECONDS - 1)
    src.rank_rows([repository])
    assert gh.calls == 2
    clock.advance(1)
    src.rank_rows([repository])
    assert gh.calls == 3   # re-probed exactly when the refreshed TTL lapses

    cold = make_repository(name="beta", repo="jesdi/beta")
    expected_as_of = datetime.fromtimestamp(clock.t, timezone.utc).isoformat()
    for _ in range(5):
        assert src.rank_rows([cold]) == ([], expected_as_of, True)
        clock.advance(1)
    assert gh.calls == 4


def test_cold_rank_timeout_starts_ttl_window_after_failure(
        src, gh, clock, repository):
    gh.on_call = lambda *_: clock.advance(120)
    gh.fail = TimeoutError("gh timed out")
    first = src.rank_rows([repository])
    assert first[0] == [] and first[2] is True
    assert src.rank_rows([repository]) == first
    assert gh.calls == 1


def test_rank_failure_does_not_overwrite_overlapping_success(
        src, gh, repository):
    gh.rows["alpha"] = [{"number": 9}]

    def first_probe_overlaps(_repositories):
        if gh.calls == 1:
            assert src.rank_rows([repository])[0] == [{"number": 9}]
            raise TimeoutError("first probe timed out")

    gh.on_call = first_probe_overlaps
    first = src.rank_rows([repository])
    assert first[0] == [{"number": 9}] and first[2] is False
    assert src.rank_rows([repository]) == first
    assert gh.calls == 2


def test_issue_description_cached_within_ttl(src, gh, clock):
    d = src.issue_description("jesdi/alpha", 73)
    assert (d["title"], d["error"]) == ("T", "")
    clock.advance(100)
    assert src.issue_description("jesdi/alpha", 73) == d
    assert gh.view_calls == 1


@pytest.mark.parametrize("payload", [None, [{"title": "oops"}]])
def test_issue_description_non_dict_payload_degrades(src, gh, payload):
    """issue_view returning valid JSON that is not a dict must land in the
    explicit-error path exactly as a fetch exception would, not 500 the
    route."""
    gh.issues[("jesdi/alpha", 73)] = payload
    d = src.issue_description("jesdi/alpha", 73)
    assert d["title"] == "" and d["error"] != ""


def test_issue_description_cache_key_includes_repo(src, gh, clock):
    """alpha#73 and beta#73 are cached independently."""
    for repo in ("jesdi/alpha", "jesdi/beta"):
        gh.issues[(repo, 73)] = {**ISSUE, "title": f"T-{repo}"}
    assert src.issue_description("jesdi/alpha", 73)["title"] == "T-jesdi/alpha"
    assert src.issue_description("jesdi/beta", 73)["title"] == "T-jesdi/beta"
    clock.advance(100)
    src.issue_description("jesdi/alpha", 73)
    src.issue_description("jesdi/beta", 73)
    assert gh.view_calls == 2


def test_issue_description_outage_reprobes_at_ttl_cadence_not_per_request(
        src, gh, clock, stale_description):
    """#83: during a gh outage every request re-shelled `gh issue view` (a
    120 s subprocess). A stale serve is re-probed once per TTL."""
    for _ in range(5):
        assert src.issue_description("jesdi/alpha", 73) == stale_description
    assert gh.view_calls == 2   # one probe on expiry, then the stale serve holds
    clock.advance(DESCRIPTION_TTL_SECONDS - 1)
    src.issue_description("jesdi/alpha", 73)
    assert gh.view_calls == 2
    clock.advance(1)
    src.issue_description("jesdi/alpha", 73)
    assert gh.view_calls == 3   # re-probed exactly when the refreshed TTL lapses


def test_cold_description_failure_retries_after_the_retry_interval(
        src, gh, clock):
    gh.fail = True
    first = src.issue_description("jesdi/alpha", 99)
    assert (first["title"], first["body"], first["url"]) == ("", "", "")
    assert first["error"] != ""
    for _ in range(4):
        assert src.issue_description("jesdi/alpha", 99) == first
    assert gh.view_calls == 1   # negative-cached, error marker kept
    clock.advance(DESCRIPTION_RETRY_SECONDS + 1)
    gh.fail = False
    d = src.issue_description("jesdi/alpha", 99)
    assert (d["title"], d["error"]) == ("T", "") and gh.view_calls == 2
    clock.advance(100)
    assert src.issue_description("jesdi/alpha", 99) == d
    assert gh.view_calls == 2   # the recovered payload gets a full TTL


def test_cold_description_timeout_starts_retry_window_after_failure(
        src, gh, clock):
    gh.on_call = lambda *_: clock.advance(120)
    gh.fail = TimeoutError("gh timed out")
    first = src.issue_description("jesdi/alpha", 99)
    assert first["error"]
    assert src.issue_description("jesdi/alpha", 99) == first
    assert gh.view_calls == 1


def test_message_less_failure_keeps_error_marker_across_retries(
        src, gh, clock):
    gh.fail = RuntimeError()
    first = src.issue_description("jesdi/alpha", 1)
    assert first["error"] != ""
    assert src.issue_description("jesdi/alpha", 1) == first
    assert gh.view_calls == 1
    clock.advance(DESCRIPTION_RETRY_SECONDS)
    again = src.issue_description("jesdi/alpha", 1)
    assert gh.view_calls == 2 and again["error"] != ""
    clock.advance(DESCRIPTION_RETRY_SECONDS)
    src.issue_description("jesdi/alpha", 1)
    assert gh.view_calls == 3


def test_description_failure_does_not_overwrite_overlapping_success(
        src, gh, clock):
    key = ("jesdi/alpha", 73)
    new = {"title": "new", "body": "new", "url": "new"}
    gh.issues[key] = {"title": "old", "body": "old", "url": "old"}
    src.issue_description(*key)

    def second_probe_overlaps(repo, number):
        if gh.view_calls == 2:
            gh.issues[key] = new
            assert src.issue_description(repo, number)["title"] == "new"
            raise TimeoutError("first probe timed out")

    gh.on_call = second_probe_overlaps
    clock.advance(DESCRIPTION_TTL_SECONDS)
    first = src.issue_description(*key)
    assert first["title"] == "new" and first["error"] == ""
    assert src.issue_description(*key) == first
    assert gh.view_calls == 3
