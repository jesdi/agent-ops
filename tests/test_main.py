import json
import subprocess
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

import dispatcher.main as main
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
    def __init__(self, cands=(), run_conclusion="", run_status_raises=False):
        self.cands = list(cands)
        self.claimed, self.released = [], []
        self.run_conclusion = run_conclusion  # "" = still running
        self.run_status_raises = run_status_raises

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
        self.calls = []  # (template, ctx) — parallel record; sent stays template-only

    def send(self, template, **ctx):
        self.sent.append(template)
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
        models=POLICY,
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
