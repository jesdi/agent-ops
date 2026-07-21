import json
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.github import Candidate
from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_WAKE, Stage, TaskState,
                               clear_waiting, has_waiting, load, mark_waiting, save)


class FakeGitHub:
    def __init__(self, cands=(), run_conclusion=""):
        self.cands = list(cands)
        self.claimed, self.released = [], []
        self.run_conclusion = run_conclusion  # "" = still running

    def candidates(self, target):
        return self.cands

    def claim(self, target, cand):
        self.claimed.append(cand.number)

    def comment(self, target, issue, body):
        pass

    def release(self, target, issue, reason):
        self.released.append((issue, reason))

    def run_status(self, target, run_id):
        return self.run_conclusion


class FakeSessions:
    def __init__(self, alive=()):
        self.alive_set = set(alive)
        self.spawned, self.resumed, self.ended = [], [], []

    def is_alive(self, issue):
        return issue in self.alive_set

    def spawn_stage(self, issue, worktree, prompt, stage_name):
        self.spawned.append((issue, stage_name))

    def resume(self, issue, worktree, message):
        self.resumed.append((issue, message))

    def capture_tail(self, issue, lines=25):
        return "…pane tail…"

    def end(self, issue):
        self.ended.append(issue)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, template, **ctx):
        self.sent.append(template)
        return 77


def cfg(tmp_path: Path) -> Config:
    return Config(
        state_dir=str(tmp_path / "state"), capacity=3,
        budget_threshold=0.8, racing_minutes=30, racing_threshold=0.95,
        session_memory="2g", session_cpus="2",
        targets=[Target(
            name="portfolio_eval", repo="jesdi/portfolio_eval",
            clone_path=str(tmp_path / "repo"),
            worktrees_path=str(tmp_path / "repo.worktrees"),
            rank_cmd="rank", setup_cmd="setup",
            verify_cmd="make e2e-slot SLOT={slot}",
            project_number=1, project_owner="jesdi",
            status_field_id="F", status_ready_option_id="R",
            status_in_progress_option_id="I",
        )],
    )


def patch_usage(monkeypatch, util=0.2):
    monkeypatch.setattr(
        main, "fetch_usage",
        lambda state_dir: UsageSnapshot(util, 120.0, "oauth"))


def patch_workspace(monkeypatch, tmp_path):
    def fake_create(target, issue, dry_run=False):
        wt = Path(target.worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        return str(wt)

    monkeypatch.setattr(main, "create_workspace", fake_create)


def deps(gh=None, sess=None):
    return main.Deps(github=gh or FakeGitHub(),
                     sessions=sess or FakeSessions(),
                     notifier=FakeNotifier())


def make_task(c, issue=42, stage=Stage.IMPLEMENT, **kw):
    wt = Path(c.targets[0].worktrees_path) / f"task-{issue}"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    ts = TaskState(issue=issue, target="portfolio_eval", stage=stage, slot=0,
                   worktree=str(wt), branch=f"agent/task-{issue}", title="t",
                   updated_at="2026-07-21T00:00:00+00:00", **kw)
    save(c.state_dir, ts)
    return wt


def replace_capacity(c, n):
    return dc_replace(c, capacity=n)


def test_new_candidate_claimed_and_spec_spawned(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))

    assert gh.claimed == [42]
    assert sess.spawned == [(42, "spec")]
    ts = load(c.state_dir, 42)
    assert ts.stage is Stage.SPEC and ts.slot == 0 and ts.title == "Add widget"
    sig = json.loads(
        (Path(c.targets[0].worktrees_path) / "task-42" / ".agent" / "stage.json")
        .read_text())
    assert sig == {"stage": "spec", "status": "working"}


def test_capacity_blocks_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    for i, issue in enumerate([1, 2, 3]):
        wt = Path(c.targets[0].worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True)
        save(c.state_dir, TaskState(issue=issue, target="portfolio_eval",
                                    stage=Stage.SPEC, slot=i, worktree=str(wt),
                                    branch=f"agent/task-{issue}", title="t",
                                    updated_at="2026-07-14T00:00:00+00:00"))
    gh = FakeGitHub([Candidate(99, "X", "u")])
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))
    assert gh.claimed == []


def test_budget_denied_blocks_spawns_and_pings_once(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "A", "u")])
    d = deps(gh, FakeSessions())
    c = cfg(tmp_path)
    main.run_pass(c, d)
    main.run_pass(c, d)  # second pass must not re-ping
    assert gh.claimed == []
    assert d.notifier.sent.count("budget_stall") == 1


def test_budget_resume_pings_once(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    patch_workspace(monkeypatch, tmp_path)
    d = deps()
    patch_usage(monkeypatch, util=0.95)
    main.run_pass(c, d)
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(c, d)
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_resume") == 1


def test_spec_done_advances_to_plan(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = Path(c.targets[0].worktrees_path) / "task-42"
    (wt / ".agent").mkdir(parents=True)
    spec = wt / "spec.md"
    spec.write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                    + "\n\n## Decisions\n\n" + "y " * 400)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md"}))
    save(c.state_dir, TaskState(issue=42, target="portfolio_eval",
                                stage=Stage.AWAITING_SPEC_REVIEW, slot=0,
                                worktree=str(wt), branch="agent/task-42",
                                title="t", updated_at="2026-07-14T00:00:00+00:00"))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert sess.spawned == [(42, "plan")]
    assert load(c.state_dir, 42).stage is Stage.PLAN


def test_dead_session_is_crash(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = Path(c.targets[0].worktrees_path) / "task-42"
    (wt / ".agent").mkdir(parents=True)
    save(c.state_dir, TaskState(issue=42, target="portfolio_eval",
                                stage=Stage.IMPLEMENT, slot=0, worktree=str(wt),
                                branch="agent/task-42", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive=set()))
    main.run_pass(c, d)
    assert gh.released == [(42, "session crashed mid-stage")]
    assert "session_crashed" in d.notifier.sent
    assert load(c.state_dir, 42).stage is Stage.FAILED
    assert wt.exists()  # worktree preserved


def test_claim_skipped_when_workspace_fails(tmp_path, monkeypatch):
    """create_workspace failure must NOT leave the board claimed (regression: orphan-claim window)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()

    def failing_workspace(target, issue, dry_run=False):
        raise RuntimeError("git fetch failed")

    monkeypatch.setattr(main, "create_workspace", failing_workspace)

    with pytest.raises(RuntimeError):
        main.run_pass(c, deps(gh, sess))

    assert gh.claimed == [], "board must NOT be claimed when workspace creation fails"
    assert load(c.state_dir, 42) is None, "no state file must exist for the stranded issue"


def test_release_called_when_claim_fails(tmp_path, monkeypatch):
    """github.claim failure after workspace+save must trigger release and leave QUEUED state (regression: orphan-claim window)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)

    class ClaimFailsGitHub(FakeGitHub):
        def claim(self, target, cand):
            raise RuntimeError("GitHub API error")

    gh = ClaimFailsGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()

    with pytest.raises(RuntimeError):
        main.run_pass(c, deps(gh, sess))

    assert any(issue == 42 for issue, _ in gh.released), "release must be called for the issue when claim fails"
    ts = load(c.state_dir, 42)
    assert ts is not None and ts.stage is Stage.QUEUED, "QUEUED state file must exist for crash-recovery path"


def test_digest(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(issue=42, target="portfolio_eval",
                                stage=Stage.AWAITING_SPEC_REVIEW, slot=0,
                                worktree="/x", branch="b", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    d = deps()
    main.send_digest(c, d)
    assert d.notifier.sent == ["daily_digest"]


def test_blocked_session_parks_and_frees_capacity(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "need a decision"}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == PARK_HUMAN and t.park_msg_id == 77
    assert t.stage is Stage.IMPLEMENT  # stage preserved while parked
    assert sess.ended == [42]
    assert "parked_question" in d.notifier.sent


def test_waiting_marker_parks_working_session(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "working"}))
    mark_waiting(c.state_dir, 42)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, 42)
    assert t.park == PARK_HUMAN
    assert not has_waiting(c.state_dir, 42)  # marker consumed


def test_awaiting_ci_parks_silently_with_run_id(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "awaiting-ci", "run_id": 4242}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == PARK_CI and t.ci_run_id == 4242
    assert sess.ended == [42]
    assert "parked_question" not in d.notifier.sent


def test_ci_completion_marks_unpark_requested(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, park=PARK_CI, ci_run_id=4242)
    gh = FakeGitHub(run_conclusion="failure")
    # No free capacity race: resumed same pass is fine — assert either state.
    main.run_pass(c, deps(gh, FakeSessions()))
    t = load(c.state_dir, 42)
    assert t.park in (PARK_WAKE, "")  # woken; may already have resumed
    if t.park == PARK_WAKE:
        assert "run 4242 concluded: failure" in t.pending_reply


def test_woken_task_resumes_before_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    wt = make_task(c, park=PARK_WAKE, pending_reply="use oauth")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert sess.resumed == [(42, "use oauth")]
    assert gh.claimed == []  # head-of-queue: resume consumed the only slot
    t = load(c.state_dir, 42)
    assert t.park == "" and t.pending_reply == "" and t.park_msg_id == 0
    sig = json.loads((wt / ".agent" / "stage.json").read_text())
    assert sig["status"] == "working"  # rewritten before resume


def test_hold_for_attach_resumes_without_reply_injection(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, park=PARK_WAKE, hold_for_attach=True)
    sess = FakeSessions()
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert len(sess.resumed) == 1 and "attaching" in sess.resumed[0][1]
    assert "resumed_for_attach" in d.notifier.sent


def test_parked_task_does_not_block_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, park=PARK_HUMAN, park_msg_id=55)
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [99]
