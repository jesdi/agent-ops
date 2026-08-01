"""Sources against tmp_path state dirs and hand-written github/session fakes."""
import json
import subprocess

from dispatcher import eventlog, state
from dispatcher.queue_ops import QueuePlan
from tests.webfakes import make_config, make_target
from web.sources import RANK_TTL_SECONDS, Sources


class FakeGitHub:
    def __init__(self):
        self.rows = {}          # target name -> list[dict]
        self.calls = 0
        self.fail = False
        self.states = {}        # (repo, number) -> "OPEN"/"CLOSED"/None
        self.boosts = []

    def rank_rows(self, target):
        self.calls += 1
        if self.fail:
            raise RuntimeError("gh exploded")
        return self.rows.get(target.name, [])

    def issue_state(self, repo, number):
        v = self.states.get((repo, number))
        if v is None:
            raise RuntimeError("gh failed")
        return v

    def set_boost(self, target, issue, value):
        self.boosts.append((target.name, issue, value))

    def set_status(self, target, issue, option_id):
        pass

    def add_label(self, target, issue, label):
        pass


class FakeSessions:
    def __init__(self):
        self.tails = {}
        self.histories = {}
        self.alive = set()

    def capture_tail(self, issue, lines=25):
        return self.tails.get(issue, "")

    def capture_history(self, issue, lines=2000):
        return self.histories.get(issue, "")

    def is_alive(self, issue):
        return issue in self.alive


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_sources(tmp_path, github=None, sessions=None, clock=None,
                 systemctl=("true",)):
    cfg = make_config(tmp_path)
    return cfg, Sources(cfg, sessions or FakeSessions(),
                        github or FakeGitHub(), clock=clock or FakeClock(),
                        systemctl=systemctl)


def test_tasks_reads_state_dir(tmp_path):
    from tests.webfakes import make_task
    state.save(tmp_path, make_task(issue=5))
    _, src = make_sources(tmp_path)
    assert [t.issue for t in src.tasks()] == [5]


def test_rank_rows_cached_within_ttl(tmp_path):
    gh = FakeGitHub()
    gh.rows["alpha"] = [{"number": 9, "title": "t", "url": "u",
                        "status": "Ready", "labels": ["auto"],
                        "blocked": False, "score": 1.0, "boost": 0}]
    clock = FakeClock()
    _, src = make_sources(tmp_path, github=gh, clock=clock)
    target = make_target()
    rows1, as_of1, stale1 = src.rank_rows(target)
    rows2, as_of2, stale2 = src.rank_rows(target)
    assert gh.calls == 1
    assert rows1 == rows2 and as_of1 == as_of2
    assert stale1 is False and stale2 is False
    clock.t += RANK_TTL_SECONDS + 1
    src.rank_rows(target)
    assert gh.calls == 2


def test_rank_failure_serves_stale_cache(tmp_path):
    gh = FakeGitHub()
    gh.rows["alpha"] = [{"number": 9, "boost": 0}]
    clock = FakeClock()
    _, src = make_sources(tmp_path, github=gh, clock=clock)
    target = make_target()
    rows, as_of, _ = src.rank_rows(target)
    clock.t += RANK_TTL_SECONDS + 1
    gh.fail = True
    rows2, as_of2, stale = src.rank_rows(target)
    assert rows2 == rows and as_of2 == as_of and stale is True


def test_rank_failure_without_cache_is_empty_stale(tmp_path):
    gh = FakeGitHub()
    gh.fail = True
    _, src = make_sources(tmp_path, github=gh)
    rows, as_of, stale = src.rank_rows(make_target())
    assert rows == [] and stale is True and as_of


def test_usage_reads_fresh_cache_only(tmp_path):
    clock = FakeClock()
    (tmp_path / "usage-cache.json").write_text(json.dumps({
        "fetched_at": clock.t,
        "snapshot": {"utilization": 0.42, "minutes_to_reset": 90.0,
                     "source": "oauth"}}))
    _, src = make_sources(tmp_path, clock=clock)
    snap = src.usage()
    assert snap.utilization == 0.42 and snap.source == "oauth"


def test_failure_and_quarantine_entries(tmp_path):
    (tmp_path / "failures").mkdir()
    (tmp_path / "failures" / "abc123def456").write_text(json.dumps(
        {"repo": "jesdi/alpha", "issue": 4, "when": "2026-07-25T09:00:00"}))
    (tmp_path / "quarantine").mkdir()
    (tmp_path / "quarantine" / "alpha-7.json").write_text(json.dumps(
        {"task_issue": 7, "blocker_repo": "jesdi/agent-ops",
         "blocker_issue": 31, "fingerprint": "abc123def456",
         "created_at": "2026-07-25T09:05:00"}))
    _, src = make_sources(tmp_path)
    fps = src.fingerprint_entries()
    assert fps == [{"fingerprint": "abc123def456", "repo": "jesdi/alpha",
                    "issue": 4, "when": "2026-07-25T09:00:00"}]
    qs = src.quarantine_entries()
    assert qs[0]["target"] == "alpha" and qs[0]["task_issue"] == 7


def test_issue_open_maps_states_and_failures(tmp_path):
    gh = FakeGitHub()
    gh.states[("r", 1)] = "OPEN"
    gh.states[("r", 2)] = "CLOSED"
    _, src = make_sources(tmp_path, github=gh)
    assert src.issue_open("r", 1) is True
    assert src.issue_open("r", 2) is False
    assert src.issue_open("r", 3) is None  # gh raised


def test_events_tail_via_real_eventlog(tmp_path):
    eventlog.append_event(tmp_path, "claimed", target="alpha", issue=7,
                          stage="spec", actor="dispatcher")
    _, src = make_sources(tmp_path)
    tail = src.events_tail(10)
    assert tail[-1]["event"] == "claimed" and tail[-1]["issue"] == 7


def test_submit_intent_writes_file_and_kicks_dispatcher(tmp_path):
    kick_log = tmp_path / "kick.log"
    _, src = make_sources(
        tmp_path,
        systemctl=("sh", "-c", f"echo kicked >> {kick_log}"))
    name = src.submit_intent("reply", 7, {"text": "hi"}, "jesdi@github")
    assert name.endswith("-7-reply.json")
    assert (tmp_path / "intents" / name).exists()
    assert kick_log.read_text().strip() == "kicked"
    pend = src.pending_intents()
    assert pend[0]["action"] == "reply" and pend[0]["issue"] == 7
    assert pend[0]["actor"] == "jesdi@github" and pend[0]["created_at"]


def test_submit_intent_survives_kick_failure(tmp_path):
    _, src = make_sources(tmp_path,
                          systemctl=("/nonexistent-binary-xyz",))
    name = src.submit_intent("park", 7, {}, "jesdi@github")
    assert (tmp_path / "intents" / name).exists()


def test_apply_queue_plan_delegates_to_queue_ops(tmp_path):
    gh = FakeGitHub()
    _, src = make_sources(tmp_path, github=gh)
    src.apply_queue_plan(make_target(), 9,
                         QueuePlan(ok=True, set_boost=3))
    assert gh.boosts == [("alpha", 9, 3)]


def test_state_fingerprint_tracks_categories(tmp_path):
    from tests.webfakes import make_task
    _, src = make_sources(tmp_path)
    f1 = json.loads(src.state_fingerprint())
    state.save(tmp_path, make_task(issue=5))
    f2 = json.loads(src.state_fingerprint())
    assert f2["board"] != f1["board"]
    assert f2["budget"] == f1["budget"]
    eventlog.append_event(tmp_path, "claimed", issue=5)
    f3 = json.loads(src.state_fingerprint())
    assert f3["history"] != f2["history"]


def test_attached_markers_roundtrip(tmp_path):
    _, src = make_sources(tmp_path)
    assert src.has_attached(7) is False
    src.mark_attached(7)
    assert src.has_attached(7) is True
    src.clear_attached(7)
    assert src.has_attached(7) is False


def test_quarantine_and_intent_entries_tolerate_oserror(tmp_path):
    """If a file is deleted or becomes unreadable after glob() but before
    read_text() (race condition during dispatcher processing), OSError
    should be caught silently. We simulate this by creating a directory
    with .json extension."""
    _, src = make_sources(tmp_path)

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

    def capture_tail(self, issue, lines=25):
        raise subprocess.TimeoutExpired(["tmux", "capture-pane"], 30)


def test_pane_tail_degrades_to_empty_when_tmux_is_wedged(tmp_path):
    """sessions.capture_tail uses subprocess.run(timeout=30), which raises.
    Unhandled it turns GET /api/task/{issue} into a 500 after 30 s."""
    sess = WedgedSessions()
    sess.alive.add(7)
    _, src = make_sources(tmp_path, sessions=sess)
    assert src.pane_tail(7) == ""


def test_pane_history_delegates_to_capture_history(tmp_path):
    sessions = FakeSessions()
    sessions.alive.add(7)
    sessions.histories = {7: "old\nnew"}
    _, src = make_sources(tmp_path, sessions=sessions)
    assert src.pane_history(7) == "old\nnew"


def test_pane_history_degrades_to_empty_on_tmux_error(tmp_path):
    class BoomSessions(FakeSessions):
        def capture_history(self, issue, lines=2000):
            raise TimeoutError("tmux wedged")
    sess = BoomSessions()
    sess.alive.add(7)
    _, src = make_sources(tmp_path, sessions=sess)
    assert src.pane_history(7) == ""


def _write_snapshot(tmp_path, issue, text):
    snap = tmp_path / "snapshots" / f"task-{issue}.txt"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(text)
    return snap


def test_pane_tail_falls_back_to_snapshot_when_dead(tmp_path):
    _write_snapshot(tmp_path, 7, "old output\nlast line")
    _, src = make_sources(tmp_path)  # 7 not in alive set
    assert src.pane_tail(7) == "old output\nlast line"


def test_pane_tail_prefers_live_capture(tmp_path):
    _write_snapshot(tmp_path, 7, "stale snapshot")
    sess = FakeSessions()
    sess.alive.add(7)
    sess.tails[7] = "live output"
    _, src = make_sources(tmp_path, sessions=sess)
    assert src.pane_tail(7) == "live output"


def test_pane_tail_dead_and_no_snapshot_degrades_to_empty(tmp_path):
    _, src = make_sources(tmp_path)
    assert src.pane_tail(7) == ""


def test_pane_history_falls_back_to_snapshot_when_dead(tmp_path):
    _write_snapshot(tmp_path, 7, "l1\nl2\nl3")
    _, src = make_sources(tmp_path)
    assert src.pane_history(7) == "l1\nl2\nl3"


def test_pane_tail_wedged_is_alive_degrades_to_empty(tmp_path):
    class WedgedSessions(FakeSessions):
        def is_alive(self, issue):
            raise subprocess.TimeoutExpired(cmd="tmux", timeout=30)

    _write_snapshot(tmp_path, 7, "snapshot")
    _, src = make_sources(tmp_path, sessions=WedgedSessions())
    assert src.pane_tail(7) == ""


def test_pane_tail_corrupt_snapshot_degrades_to_empty(tmp_path):
    """Snapshot truncated mid-UTF-8 (e.g. by dispatcher kill) raises
    UnicodeDecodeError; must degrade to "" not 500 the task view."""
    snap = tmp_path / "snapshots" / "task-7.txt"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(b"\xff\xfe\xff")  # Invalid UTF-8 sequence
    _, src = make_sources(tmp_path)
    assert src.pane_tail(7) == ""


def test_pane_history_corrupt_snapshot_degrades_to_empty(tmp_path):
    """Same as pane_tail: corrupt snapshot with invalid UTF-8 degrades."""
    snap = tmp_path / "snapshots" / "task-7.txt"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(b"\x80\x81\x82")  # Invalid UTF-8 sequence
    _, src = make_sources(tmp_path)
    assert src.pane_history(7) == ""


def test_pass_heartbeat_reads_and_degrades(tmp_path):
    _, src = make_sources(tmp_path)
    assert src.pass_heartbeat() is None
    src.state_dir.mkdir(parents=True, exist_ok=True)
    (src.state_dir / "pass.json").write_text('{"interval_minutes": 10}')
    assert src.pass_heartbeat() == {"interval_minutes": 10}
    (src.state_dir / "pass.json").write_text("not json")
    assert src.pass_heartbeat() is None


def test_state_fingerprint_changes_when_pass_json_changes(tmp_path):
    _, src = make_sources(tmp_path)
    f1 = json.loads(src.state_fingerprint())
    (tmp_path / "pass.json").write_text('{"interval_minutes": 10}')
    f2 = json.loads(src.state_fingerprint())
    assert f2["board"] != f1["board"]


def test_claims_paused_and_triage_running_degrade_to_false(tmp_path):
    """Both triage flags degrade to False on any error; never block claims."""
    _, src = make_sources(tmp_path)
    # Without any triage process or request file, both return False.
    assert src.claims_paused() is False
    assert src.triage_running() is False
