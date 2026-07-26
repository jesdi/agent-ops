"""Sources against tmp_path state dirs and hand-written github/session fakes."""
import json

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
        self.alive = set()

    def capture_tail(self, issue, lines=25):
        return self.tails.get(issue, "")

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
