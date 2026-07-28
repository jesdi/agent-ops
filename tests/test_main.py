import json
import subprocess
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import failures
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_WAKE, Stage, TaskState,
                               clear_waiting, has_waiting, load, mark_waiting, save)

POLICY = parse_policy({
    "default": "claude-opus-4-8",
    "rules": [
        {"name": "trivial-backend",
         "when": {"effort": {"max": 1}, "labels_exclude": ["frontend"]},
         "use": "claude-sonnet-4-6"},
        {"name": "frontend-substantial",
         "when": {"effort": {"min": 2}, "labels_include": ["frontend"]},
         "use": {"spec": "claude-fable-5", "plan": "claude-fable-5",
                 "implement": "claude-opus-4-8"}},
    ],
})


class FakeGitHub:
    def __init__(self, cands=(), run_conclusion="", run_status_raises=False,
                 issue_states=None, issue_state_raises=False, rows=()):
        self.cands = list(cands)
        self.claimed, self.released = [], []
        self.run_conclusion = run_conclusion  # "" = still running
        self.run_status_raises = run_status_raises
        self.created_issues = []              # (repo, title, body)
        self.next_issue = 500
        self.issue_states = issue_states or {}  # (repo, number) -> state
        self.issue_state_raises = issue_state_raises
        self.blocked_by = []                  # (issue, blocker)
        self.rows = list(rows)
        self.boosts, self.labeled, self.statused = [], [], []
        self.boost_raises = False
        self.lookup_raises = False

    def rank_rows(self, target):
        if self.boost_raises:
            raise subprocess.CalledProcessError(1, ["rank"])
        return self.rows

    def set_boost(self, target, issue, value):
        if self.lookup_raises:
            raise LookupError(f"issue {issue} not found in project items")
        self.boosts.append((issue, value))

    def add_label(self, target, issue, label):
        self.labeled.append((issue, label))

    def set_status(self, target, issue, option_id):
        self.statused.append((issue, option_id))

    def candidates(self, target):
        return self.cands

    def claim(self, target, cand):
        self.claimed.append(cand.number)

    def comment(self, target, issue, body):
        pass

    def release(self, target, issue, reason):
        self.released.append((issue, reason))

    def run_status(self, target, run_id):
        if self.run_status_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        return self.run_conclusion

    def create_issue(self, repo, title, body):
        self.created_issues.append((repo, title, body))
        self.next_issue += 1
        return self.next_issue

    def issue_state(self, repo, number):
        if self.issue_state_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        return self.issue_states.get((repo, number), "OPEN")

    def append_blocked_by(self, target, issue, blocker):
        self.blocked_by.append((issue, blocker))


class FakeSessions:
    def __init__(self, alive=()):
        self.alive_set = set(alive)
        self.spawned, self.resumed, self.ended = [], [], []

    def is_alive(self, issue):
        return issue in self.alive_set

    def spawn_stage(self, issue, worktree, prompt, stage_name, model):
        self.spawned.append((issue, stage_name, model))

    def resume(self, issue, worktree, message, model):
        self.resumed.append((issue, message, model))

    def capture_tail(self, issue, lines=25):
        return "…pane tail…"

    def end(self, issue):
        self.ended.append(issue)


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.contexts = []
        self.calls = []  # (template, ctx) — parallel record; sent stays template-only

    def send(self, template, **ctx):
        self.sent.append(template)
        self.contexts.append((template, ctx))
        self.calls.append((template, ctx))
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
        infra_repo="jesdi/agent-ops",
        models=POLICY,
    )


def patch_usage(monkeypatch, util=0.2):
    monkeypatch.setattr(
        main, "fetch_usage",
        lambda state_dir: UsageSnapshot(util, 120.0, "oauth"))


def row(number, title="t", status="Ready", labels=("auto",), blocked=False,
        score=1.0, boost=0):
    return {"number": number, "title": title, "url": f"u{number}",
            "status": status, "labels": list(labels), "blocked": blocked,
            "score": score, "boost": boost}


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
    assert sess.spawned == [(42, "spec", "claude-opus-4-8")]
    ts = load(c.state_dir, 42)
    assert ts.stage is Stage.SPEC and ts.slot == 0 and ts.title == "Add widget"
    sig = json.loads(
        (Path(c.targets[0].worktrees_path) / "task-42" / ".agent" / "stage.json")
        .read_text())
    assert sig == {"stage": "spec", "status": "working", "model": "claude-opus-4-8"}


def test_claim_snapshots_effort_and_labels(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=3,
                               labels=("auto", "frontend"))])
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh))
    t = load(c.state_dir, 42)
    assert t.effort == 3
    assert t.labels == ("auto", "frontend")


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


def test_awaiting_review_persists_spec_artifact(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC)
    spec_path = str(wt / "docs" / "x-design.md")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-review", "note": "spec ready",
         "artifact": spec_path}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, 42)
    assert t.stage is Stage.AWAITING_SPEC_REVIEW
    assert t.artifact == spec_path


def test_gate_respawn_clears_stale_artifact(tmp_path, monkeypatch):
    # Reboot recovery: gate-parked task with a dead session re-spawns SPEC;
    # the stale artifact path must not survive into the fresh attempt.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.AWAITING_SPEC_REVIEW,
              artifact=str(tmp_path / "old-spec.md"))
    sess = FakeSessions()  # session not alive
    main.run_pass(c, deps(sess=sess))
    assert [(i, s) for i, s, _m in sess.spawned] == [(42, "spec")]
    assert load(c.state_dir, 42).artifact == ""


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
    assert sess.spawned == [(42, "plan", "claude-opus-4-8")]
    assert load(c.state_dir, 42).stage is Stage.PLAN


def test_plan_format_failure_retries_in_session_then_fails(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = Path(c.targets[0].worktrees_path) / "task-42"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".agent" / "plan.md").write_text("# t\n\n## Task 1: a\n\n" + "z " * 900)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "note": "", "artifact": ".agent/plan.md"}))
    save(c.state_dir, TaskState(issue=42, target="portfolio_eval",
                                stage=Stage.PLAN, slot=0, worktree=str(wt),
                                branch="agent/task-42", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    # First pass: resumed in place (zombie ended first), not failed.
    assert 42 in sess.ended
    assert len(sess.resumed) == 1 and sess.resumed[0][0] == 42
    assert "plan_retry" in d.notifier.sent
    t = load(c.state_dir, 42)
    assert t.stage is Stage.PLAN and t.plan_retries == 1
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"

    # Session re-signals done but the plan is still malformed → retry exhausted.
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "note": "", "artifact": ".agent/plan.md"}))
    d2 = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d2)
    assert load(c.state_dir, 42).stage is Stage.FAILED
    assert "artifact_failed" in d2.notifier.sent


def test_stage_advance_ends_previous_session_before_spawn(tmp_path, monkeypatch):
    """An interactive claude cannot exit itself, so on stage advance the
    previous session is usually still alive. Spawning without ending it
    first makes _launch type the next stage's podman command INTO the old
    claude's input box (and the container name would collide anyway) —
    task #192's plan stage was 'spawned' straight into the zombie spec
    session. End first, then spawn."""
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
                                stage=Stage.SPEC, slot=0,
                                worktree=str(wt), branch="agent/task-42",
                                title="t", updated_at="2026-07-14T00:00:00+00:00"))

    class OrderedSessions(FakeSessions):
        def __init__(self, alive=()):
            super().__init__(alive)
            self.ops = []

        def end(self, issue):
            self.ops.append(("end", issue))
            super().end(issue)

        def spawn_stage(self, issue, worktree, prompt, stage_name, model):
            self.ops.append(("spawn", stage_name))
            super().spawn_stage(issue, worktree, prompt, stage_name, model)

    sess = OrderedSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert sess.ops == [("end", 42), ("spawn", "plan")]


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


def test_dead_session_files_diagnosis_issue_and_blocks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive=set()))
    main.run_pass(c, d)

    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/portfolio_eval", "session crashes file on the TARGET repo"
    assert "session-crash" in title and "implement" in title
    assert "- class: session-crash" in body
    assert "…pane tail…" in body  # FakeSessions.capture_tail
    assert gh.blocked_by == [(42, 501)]
    assert "task_failed" in d.notifier.sent
    # existing crash handling still intact
    assert gh.released == [(42, "session crashed mid-stage")]
    assert load(c.state_dir, 42).stage is Stage.FAILED


def test_workspace_failure_reports_quarantines_and_pass_survives(tmp_path, monkeypatch):
    """create_workspace failure: no claim, no state file, one issue + one
    ping, quarantine written, and the pass exits cleanly (regression: one
    bad candidate killed the pass). Capped at one provisioning failure per
    target per pass, so the NEXT candidate (43) is NOT claimed this pass —
    a systemic fault is far likelier than a per-candidate one, and the next
    pass retries 43."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Bad", "u42"), Candidate(43, "Good", "u43")])
    sess = FakeSessions()

    def failing_workspace(target, issue, dry_run=False):
        if issue == 42:
            raise RuntimeError("pipenv: no python 3.13")
        wt = Path(target.worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        return str(wt)

    monkeypatch.setattr(main, "create_workspace", failing_workspace)
    d = deps(gh, sess)
    main.run_pass(c, d)  # must NOT raise

    assert gh.claimed == [], "capped at one provisioning failure per pass; 43 not claimed this pass"
    assert load(c.state_dir, 42) is None, "no state file for the failed candidate"
    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops" and "provisioning" in title
    assert "no python 3.13" in body
    assert d.notifier.sent.count("task_failed") == 1
    rec = json.loads(failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).read_text())
    assert rec["blocker_issue"] == 501 and rec["blocker_repo"] == "jesdi/agent-ops"


def test_workspace_failure_second_pass_dedupes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Bad", "u42")])

    def failing_workspace(target, issue, dry_run=False):
        raise RuntimeError("pipenv: no python 3.13")

    monkeypatch.setattr(main, "create_workspace", failing_workspace)
    d = deps(gh, FakeSessions())
    main.run_pass(c, d)
    # Simulate a force-retry (record deleted) with the same failure: the
    # fingerprint dedupes, and the quarantine is rewritten with the
    # ORIGINAL blocker number read back from the marker.
    failures.quarantine_path(c.state_dir, "portfolio_eval", 42).unlink()
    main.run_pass(c, d)
    assert len(gh.created_issues) == 1
    assert d.notifier.sent.count("task_failed") == 1
    rec = json.loads(failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).read_text())
    assert rec["blocker_issue"] == 501


def test_quarantined_candidate_with_open_blocker_skipped(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Bad", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []


def test_closed_blocker_unquarantines_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Fixed now", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "CLOSED"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [42], "candidate claimable in the SAME pass"
    assert not failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).exists()


def test_issue_state_error_keeps_candidate_quarantined(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Bad", "u42"), Candidate(43, "Good", "u43")],
                    issue_state_raises=True)
    main.run_pass(c, deps(gh, FakeSessions()))  # must not raise
    assert gh.claimed == [43]


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
    assert sess.resumed == [(42, "use oauth", "claude-opus-4-8")]
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


from telegram.inbound import Command, Plain, Reply


def patch_events(monkeypatch, events):
    monkeypatch.setattr(main.inbound, "fetch_events", lambda sd: list(events))


def test_reply_wakes_matching_parked_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot
    patch_events(monkeypatch, [Reply(reply_to_msg_id=55, text="use oauth")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE and t.pending_reply == "use oauth"


def test_reply_to_unknown_message_is_reported(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    patch_events(monkeypatch, [Reply(reply_to_msg_id=999, text="hi")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent  # "(reply didn't match any parked task)"


def test_plain_text_with_single_parked_task_wakes_it(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Plain(text="go ahead")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    assert load(c.state_dir, 42).park == PARK_WAKE


def test_plain_text_with_two_parked_tasks_asks_which(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43, park=PARK_HUMAN, park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, 42).park == PARK_HUMAN
    assert load(c.state_dir, 43).park == PARK_HUMAN
    assert "status" in d.notifier.sent  # the "Which task?" prompt


def test_attach_command_sets_hold(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE and t.hold_for_attach is True


def test_status_command_reports(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_CI, ci_run_id=7)
    patch_events(monkeypatch, [Command(name="status")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent


def test_pass_crash_files_issue_and_reraises(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions())

    def boom(cfg_, deps_, dry_run=False):
        raise RuntimeError("rank.py exploded")

    monkeypatch.setattr(main, "run_pass", boom)
    with pytest.raises(RuntimeError):
        main.guarded_pass(c, d, "targets.yaml")

    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops"
    assert "pass-crash" in title and "(dispatcher)" in title
    assert "rank.py exploded" in body
    assert "agent-ops-dispatcher --config targets.yaml" in body
    assert "task_failed" in d.notifier.sent


def test_pass_crash_recurring_dedupes_to_one_issue(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions())
    monkeypatch.setattr(main, "run_pass",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("rank.py exploded")))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            main.guarded_pass(c, d, "targets.yaml")
    assert len(gh.created_issues) == 1
    assert d.notifier.sent.count("task_failed") == 1


def test_run_status_error_does_not_abort_pass(tmp_path, monkeypatch):
    """A gh run_status failure must be treated as 'still running': skip that task,
    keep the pass going (e.g., a second claimable candidate is still claimed)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    # Parked CI task whose run_status call will raise CalledProcessError
    make_task(c, issue=42, park=PARK_CI, ci_run_id=4242)
    # A fresh candidate available to be claimed
    gh = FakeGitHub(cands=[Candidate(99, "fresh", "u")], run_status_raises=True)
    sess = FakeSessions()
    # Must NOT raise; the pass must survive the error
    main.run_pass(c, deps(gh, sess))
    # CI-parked task remains unchanged (still PARK_CI with same run_id)
    t = load(c.state_dir, 42)
    assert t.park == PARK_CI and t.ci_run_id == 4242
    # The rest of the pass continued: the fresh candidate was claimed
    assert gh.claimed == [99]


def test_queue_command_sends_ranked_view(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(2, title="B", score=5.0, boost=1),
                          row(1, title="A", score=1.0),
                          row(3, title="C", score=None),
                          row(4, title="D", blocked=True),
                          row(5, title="E", status="In progress")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)
    template, ctx = d.notifier.contexts[0]
    assert template == "queue"
    lines = ctx["lines"]
    assert lines[0] == "1. ↑1 [5.00] #2 B"
    assert lines[1] == "2. [1.00] #1 A"
    assert lines[2] == "3. [—] #3 C"
    assert "In progress: #5" in lines
    assert "Blocked: #4" in lines


def test_boost_command_adjusts_and_confirms(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, boost=2)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=-3)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [(7, -1)]
    assert any("#7 boost 2 → -1" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_boost_unknown_issue_replies_not_on_board(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=99, amount=1)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == []
    assert any("#99 is not on the board" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_eligible_sets_head_boost(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [(7, 99)]
    assert gh.statused == [] and gh.labeled == []


def test_next_ineligible_refuses_with_reason_and_hint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == []
    joined = " ".join(l for _, ctx in d.notifier.contexts
                      for l in ctx.get("lines", []))
    assert "not eligible" in joined and "Backlog" in joined
    assert "auto" in joined and "/next 7 force" in joined


def test_next_force_flips_status_and_label_then_boosts(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.statused == [(7, "R")]   # status_ready_option_id in cfg() is "R"
    assert gh.labeled == [(7, "auto")]
    assert gh.boosts == [(7, 99)]


def test_next_blocked_never_forced(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, blocked=True)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("blocked" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_boost_ambiguous_across_targets_asks_to_disambiguate(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    c = dc_replace(c, targets=[c.targets[0],
                               dc_replace(c.targets[0], name="other")])
    gh = FakeGitHub(rows=[row(7)])   # rank_rows returns #7 for BOTH targets
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(c, d)
    assert gh.boosts == []
    assert any("multiple targets" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_gh_failure_reports_error_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.boost_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_lookup_error_boost_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("boost failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_lookup_error_next_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("next failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_value_error_queue_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)

    def bad_rank_rows(target):
        raise ValueError("malformed JSON from rank output")

    gh = FakeGitHub(rows=[row(7)])
    monkeypatch.setattr(gh, "rank_rows", bad_rank_rows)
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("/queue failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_in_progress_refused_without_force_hint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="In progress")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    joined = " ".join(l for _, ctx in d.notifier.contexts
                      for l in ctx.get("lines", []))
    assert "In progress" in joined
    assert "force" not in joined.replace("cannot be forced", "")


def test_next_in_progress_never_forced(tmp_path, monkeypatch):
    """The board is the double-dispatch guard: /next force must never flip a
    claimed issue back to Ready."""
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="In progress", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("In progress" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_force_boost_failure_leaves_board_unmutated(tmp_path, monkeypatch):
    """set_boost runs first, so a Boost-field failure is a clean no-op."""
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("#7 next failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_queue_shows_demote_marker_and_truncates(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    rows = [row(1, title="A", score=9.0, boost=-3)]
    rows += [row(n, title=f"T{n}", score=1.0) for n in range(2, 14)]
    d = deps(FakeGitHub(rows=rows))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)
    lines = d.notifier.contexts[0][1]["lines"]
    assert lines[0] == "1. ↓3 [9.00] #1 A"
    assert len([l for l in lines if l[0].isdigit()]) == 10
    assert "… 3 more" in lines


def test_queue_labels_each_target_and_reports_empty(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    c = dc_replace(c, targets=[c.targets[0],
                               dc_replace(c.targets[0], name="other")])
    d = deps(FakeGitHub(rows=[]))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(c, d)
    lines = d.notifier.contexts[0][1]["lines"]
    assert lines == ["[portfolio_eval]", "(queue empty)",
                     "[other]", "(queue empty)"]


def valid_spec(wt: Path) -> Path:
    """A spec draft that satisfies check_spec (title + 2 H2s + >=1500B)."""
    p = wt / "spec.md"
    p.write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                 + "\n\n## Decisions\n\n" + "y " * 400)
    return p


def test_spawn_uses_the_rule_matched_model(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=1, labels=("auto",))])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))
    assert sess.spawned == [(42, "spec", "claude-sonnet-4-6")]


def test_unmatched_task_spawns_on_the_default_model(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=None, labels=("auto",))])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))
    assert sess.spawned == [(42, "spec", "claude-opus-4-8")]


def test_frontend_task_spawns_spec_on_fable(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=3,
                               labels=("auto", "frontend"))])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))
    assert sess.spawned == [(42, "spec", "claude-fable-5")]


def test_frontend_task_spawns_plan_on_fable_and_implement_on_opus(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW,
                   effort=3, labels=("auto", "frontend"))
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert sess.spawned == [(42, "plan", "claude-fable-5")]

    # …and the implement stage of the same task drops to opus
    t = load(c.state_dir, 42)
    assert main._model_for(c, c.targets[0], t, Stage.IMPLEMENT) == "claude-opus-4-8"


def test_resume_uses_the_model_for_the_parked_stage(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT, park=PARK_WAKE,
              pending_reply="carry on", effort=1, labels=("auto",))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [(42, "carry on", "claude-sonnet-4-6")]


def test_spawn_writes_model_into_stage_json_and_models_log(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=1, labels=("auto",))])
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh))
    agent_dir = Path(c.targets[0].worktrees_path) / "task-42" / ".agent"
    sig = json.loads((agent_dir / "stage.json").read_text())
    assert sig["model"] == "claude-sonnet-4-6"
    log = (agent_dir / "models.log").read_text().strip().splitlines()
    assert len(log) == 1
    assert log[0].endswith(" spec claude-sonnet-4-6")


def test_models_log_appends_one_line_per_spawn(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW,
                   effort=1, labels=("auto",))
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    log = (wt / ".agent" / "models.log").read_text().strip().splitlines()
    assert len(log) == 1
    assert log[0].endswith(" plan claude-sonnet-4-6")


def test_status_lines_carry_the_resolved_model(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, target="portfolio_eval", stage=Stage.IMPLEMENT, slot=0,
        worktree="/wt", branch="agent/task-42", title="Fix rounding",
        updated_at="2026-07-24T00:00:00+00:00", effort=1, labels=("auto",)))
    save(c.state_dir, TaskState(
        issue=43, target="portfolio_eval", stage=Stage.SPEC, slot=1,
        worktree="/wt2", branch="agent/task-43", title="New chart",
        updated_at="2026-07-24T00:00:00+00:00", effort=3,
        labels=("auto", "frontend")))
    lines = main._status_lines(c)
    assert lines[0] == "#42 Fix rounding — implement [claude-sonnet-4-6] (slot 0)"
    assert lines[1] == "#43 New chart — spec [claude-fable-5] (slot 1)"


def test_status_line_for_task_parked_at_spec_review_shows_the_spec_model(tmp_path):
    """Finding A: a frontend task lingering at the spec-review gate is still
    running its SPEC session — /status must show that stage's model, not
    fall through the policy default because 'awaiting-spec-review' matches
    no `use:` key."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, target="portfolio_eval", stage=Stage.AWAITING_SPEC_REVIEW,
        slot=0, worktree="/wt", branch="agent/task-42", title="New chart",
        updated_at="2026-07-24T00:00:00+00:00", effort=3,
        labels=("auto", "frontend")))
    lines = main._status_lines(c)
    assert lines[0] == "#42 New chart — awaiting-spec-review [claude-fable-5] (slot 0)"


def test_resume_at_spec_review_gate_uses_the_spec_model(tmp_path, monkeypatch):
    """Finding A: resuming a task parked at the spec-review gate must run the
    same model as the spec session that's actually alive, not the policy
    default that `stage.value` ('awaiting-spec-review') falls through to."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, park=PARK_WAKE,
              pending_reply="carry on", effort=3, labels=("auto", "frontend"))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [(42, "carry on", "claude-fable-5")]


def test_status_lines_survive_a_task_whose_target_is_gone(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=44, target="retired_target", stage=Stage.PLAN, slot=0,
        worktree="/wt", branch="agent/task-44", title="Orphan",
        updated_at="2026-07-24T00:00:00+00:00", effort=1, labels=("auto",)))
    lines = main._status_lines(c)
    assert "[claude-sonnet-4-6]" in lines[0]   # falls back to the global policy


def test_orphaned_task_at_the_spec_review_gate_still_maps_to_the_spec_model(
        tmp_path):
    # The global-policy fallback must go through the same stage mapping as the
    # normal path, or a frontend task parked at the gate misreports as opus.
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=45, target="retired_target", stage=Stage.AWAITING_SPEC_REVIEW,
        slot=0, worktree="/wt", branch="agent/task-45", title="Orphan chart",
        updated_at="2026-07-24T00:00:00+00:00", effort=3,
        labels=("auto", "frontend")))
    lines = main._status_lines(c)
    assert lines[0] == ("#45 Orphan chart — awaiting-spec-review "
                        "[claude-fable-5] (slot 0)")


def test_status_line_for_a_parked_task_puts_model_before_park(tmp_path):
    """Finding C: the format is
    '#{issue} {title} — {stage} [{model}] [{park}] (slot {slot})' — the
    [model] segment must come before the pre-existing [park] segment."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, target="portfolio_eval", stage=Stage.IMPLEMENT, slot=0,
        worktree="/wt", branch="agent/task-42", title="Fix rounding",
        updated_at="2026-07-24T00:00:00+00:00", effort=1, labels=("auto",),
        park=PARK_HUMAN))
    lines = main._status_lines(c)
    assert lines[0] == ("#42 Fix rounding — implement [claude-sonnet-4-6] "
                        f"[{PARK_HUMAN}] (slot 0)")


def test_status_reply_content_carries_the_status_lines(tmp_path, monkeypatch):
    """Finding C: FakeNotifier previously recorded only the template name, so
    no test could assert on what a /status reply actually contains."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_CI, ci_run_id=7)
    patch_events(monkeypatch, [Command(name="status")])
    d = deps()
    main.run_pass(c, d)
    template, ctx = [call for call in d.notifier.calls if call[0] == "status"][-1]
    assert any(line.startswith("#42 ") for line in ctx["lines"])


def test_digest_content_carries_the_status_lines(tmp_path):
    """Finding C: the digest's payload was likewise never asserted on."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(issue=42, target="portfolio_eval",
                                stage=Stage.AWAITING_SPEC_REVIEW, slot=0,
                                worktree="/x", branch="b", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    d = deps()
    main.send_digest(c, d)
    template, ctx = d.notifier.calls[-1]
    assert template == "daily_digest"
    assert any(line.startswith("#42 ") for line in ctx["lines"])


def test_target_specific_policy_is_used_at_spawn(tmp_path, monkeypatch):
    """Finding D: a per-target `models:` override was only ever verified in
    isolation against `policy_for` — never end-to-end through `_spawn_stage`.
    The global POLICY's `trivial-backend` rule would resolve effort=1,
    no-frontend to claude-sonnet-4-6; the target's OWN policy (no rules)
    must win instead and resolve to its own default."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    target_policy = parse_policy({"default": "claude-fable-5", "rules": []})
    c = dc_replace(c, targets=[dc_replace(c.targets[0], models=target_policy)])
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=1, labels=("auto",))])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert sess.spawned == [(42, "spec", "claude-fable-5")]


from dispatcher import eventlog


def test_claim_and_spawn_append_claimed_and_stage_started_events(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    main.run_pass(c, deps(FakeGitHub([Candidate(42, "Add widget", "u42")])))
    events = eventlog.read_tail(c.state_dir)
    assert [(e["event"], e["issue"]) for e in events] == [
        ("claimed", 42), ("stage-started", 42)]
    assert events[0]["target"] == "portfolio_eval"
    assert events[1]["stage"] == "spec"
    assert events[1]["model"] == "claude-opus-4-8"


def test_park_for_input_appends_parked_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "need a decision"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    parked = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "parked"]
    assert len(parked) == 1
    assert parked[0]["issue"] == 42 and parked[0]["stage"] == "implement"
    assert parked[0]["detail"] == "need a decision"


def test_park_for_ci_appends_parked_event_with_run_id_detail(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "awaiting-ci", "run_id": 4242}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    parked = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "parked"]
    assert parked[0]["detail"] == "awaiting CI run 4242"


def test_resume_appends_resumed_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, park=PARK_WAKE, pending_reply="use oauth")
    main.run_pass(c, deps(sess=FakeSessions()))
    resumed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "resumed"]
    assert len(resumed) == 1
    assert resumed[0]["issue"] == 42 and resumed[0]["stage"] == "implement"
    assert resumed[0]["model"] == "claude-opus-4-8"


def test_implement_done_appends_pr_opened_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "done", "note": "PR #9"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, 42).stage is Stage.PR_OPEN
    opened = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "pr-opened"]
    assert len(opened) == 1 and opened[0]["issue"] == 42


def test_crash_appends_failed_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT)
    main.run_pass(c, deps(FakeGitHub(), FakeSessions(alive=set())))
    failed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["issue"] == 42
    assert failed[0]["detail"] == "session crashed mid-stage"


from dispatcher import intents as intents_mod


def test_reply_intent_wakes_parked_task_and_is_deleted(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot → 42 stays PARK_WAKE
    intents_mod.write_intent(c.state_dir, "reply", 42, {"text": "use oauth"},
                             actor="jesdi@github", epoch_ms=1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE and t.pending_reply == "use oauth"
    assert intents_mod.list_intents(c.state_dir) == []
    applied = [e for e in eventlog.read_tail(c.state_dir)
               if e["event"] == "intent-applied"]
    assert applied[0]["actor"] == "jesdi@github"
    assert applied[0]["detail"] == "reply" and applied[0]["issue"] == 42


def test_reply_intent_for_unparked_task_is_skipped_and_deleted(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)  # not parked
    intents_mod.write_intent(c.state_dir, "reply", 42, {"text": "hi"}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, 42).park == ""  # untouched
    assert intents_mod.list_intents(c.state_dir) == []  # still deleted


def test_park_intent_parks_a_live_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents_mod.write_intent(c.state_dir, "park", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == PARK_HUMAN and t.park_msg_id == 77
    assert sess.ended == [42]
    assert "parked_question" in d.notifier.sent
    assert intents_mod.list_intents(c.state_dir) == []


def test_kill_intent_ends_releases_and_tombstones_state(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    mark_waiting(c.state_dir, 42)
    intents_mod.write_intent(c.state_dir, "kill", 42, {}, "op", 1)
    gh = FakeGitHub()
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(gh, sess))
    assert sess.ended == [42]
    assert gh.released == [(42, "abandoned by operator")]
    assert load(c.state_dir, 42).stage is Stage.FAILED
    assert not has_waiting(c.state_dir, 42)
    failed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "failed"]
    assert failed[0]["detail"] == "killed by operator"


def test_kill_intent_survives_release_failure(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)

    class ReleaseFailsGitHub(FakeGitHub):
        def release(self, target, issue, reason):
            raise RuntimeError("gh outage")

    intents_mod.write_intent(c.state_dir, "kill", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(ReleaseFailsGitHub(), sess))  # must not raise
    assert sess.ended == [42]
    assert load(c.state_dir, 42).stage is Stage.FAILED  # tombstone written even when release raises


def test_kill_intent_does_not_reclaim_released_issue_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents_mod.write_intent(c.state_dir, "kill", 42, {}, "op", 1)
    # release() flips #42 back to Ready+auto, so it is still a live board
    # candidate this pass; the FAILED tombstone must stop _claim_new from
    # re-claiming the issue the operator just killed.
    gh = FakeGitHub([Candidate(42, "Killed but still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions(alive={42})))
    assert gh.claimed == []
    assert load(c.state_dir, 42).stage is Stage.FAILED


def test_retry_intent_clears_quarantine_and_fingerprint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    failures.fingerprint_path(c.state_dir, "abc").parent.mkdir(
        parents=True, exist_ok=True)
    failures.fingerprint_path(c.state_dir, "abc").write_text(
        json.dumps({"repo": "jesdi/agent-ops", "issue": 501, "when": "w"}))
    intents_mod.write_intent(c.state_dir, "retry", 42, {}, "op", 1)
    gh = FakeGitHub([Candidate(42, "Retry me", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert not failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).exists()
    assert not failures.fingerprint_path(c.state_dir, "abc").exists()
    assert gh.claimed == [42], "cleared before _claim_new → claimable same pass"


def test_resume_intent_wakes_with_default_text(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    intents_mod.write_intent(c.state_dir, "resume", 42, {}, "op", 1)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [
        (42, "The operator resumed this task. Continue.", "claude-opus-4-8")]


def test_resume_intent_carries_optional_text(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    intents_mod.write_intent(c.state_dir, "resume", 42, {"text": "ship it"},
                             "op", 1)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [(42, "ship it", "claude-opus-4-8")]


def test_failed_intent_does_not_abort_pass_or_remaining_intents(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 2)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43, park=PARK_HUMAN, park_msg_id=56)
    make_task(c, issue=44)  # active; keeps 42/43 at PARK_WAKE... capacity 2 → one resumes

    real_wake = main._wake

    def wake_or_boom(cfg_, task, text, hold=False):
        if task.issue == 42:
            raise RuntimeError("disk full")
        real_wake(cfg_, task, text, hold=hold)

    monkeypatch.setattr(main, "_wake", wake_or_boom)
    intents_mod.write_intent(c.state_dir, "resume", 42, {}, "op", 1)
    intents_mod.write_intent(c.state_dir, "resume", 43, {}, "op", 2)
    main.run_pass(c, deps(sess=FakeSessions(alive={44})))  # must not raise
    assert load(c.state_dir, 42).park == PARK_HUMAN  # failed intent: unchanged
    assert load(c.state_dir, 43).park in (PARK_WAKE, "")  # later intent applied
    assert intents_mod.list_intents(c.state_dir) == []  # both files deleted


def test_dry_run_does_not_drain_intents(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    intents_mod.write_intent(c.state_dir, "resume", 42, {}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions()), dry_run=True)
    assert len(intents_mod.list_intents(c.state_dir)) == 1  # untouched


from dispatcher.state import clear_attached, mark_attached


def test_attached_marker_holds_resume(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE, pending_reply="go")
    mark_attached(c.state_dir, 42)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == []
    assert load(c.state_dir, 42).park == PARK_WAKE  # still queued to wake


def test_attached_marker_holds_drive_no_crash_no_park(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.IMPLEMENT)
    # Session dead AND a blocked signal: without the marker this pass would
    # crash-handle or park. With it: nothing happens.
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    mark_attached(c.state_dir, 42)
    gh = FakeGitHub()
    sess = FakeSessions(alive=set())
    d = deps(gh, sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.stage is Stage.IMPLEMENT and t.park == ""  # untouched
    assert gh.released == [] and sess.ended == []
    assert "session_crashed" not in d.notifier.sent
    assert "parked_question" not in d.notifier.sent


def test_clearing_attached_marker_releases_the_hold(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE, pending_reply="go")
    mark_attached(c.state_dir, 42)
    sess = FakeSessions()
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert sess.resumed == []
    clear_attached(c.state_dir, 42)
    main.run_pass(c, d)
    assert sess.resumed == [(42, "go", "claude-opus-4-8")]
