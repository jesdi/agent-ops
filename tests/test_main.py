import json
import os
import subprocess
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import failures, spec_publish
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import (NO_SLOT, PARK_CI, PARK_HUMAN, PARK_LOGIN,
                               PARK_REVIEW, PARK_WAKE, Stage,
                               TaskState, clear_waiting, has_waiting, load,
                               load_all, mark_attached, mark_waiting, save)

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
        self.claimed, self.released, self.canceled = [], [], []
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
        self.pr_payloads = {}        # pr_number -> gh pr view payload
        self.pr_view_raises = False
        self.branch_prs = {}         # branch -> pr number
        self.deleted_branches = []   # (repo, branch)
        self.login = "agent-bot"
        self.comments = []           # (issue, body)
        self.comment_raises = False

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
        if self.comment_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        self.comments.append((issue, body))

    def release(self, target, issue, reason):
        self.released.append((issue, reason))

    def cancel(self, target, issue):
        self.canceled.append(issue)

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

    def viewer_login(self):
        return self.login

    def pr_view(self, target, pr_number):
        if self.pr_view_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        # Unknown PRs read as quiet/open, so tests that merely pass through
        # a pr-open task (e.g. the capture test) never crash the pass.
        return self.pr_payloads.get(pr_number, {
            "state": "OPEN", "mergedAt": None, "reviewDecision": "",
            "reviews": [], "comments": []})

    def pr_number_for_branch(self, target, branch):
        return self.branch_prs.get(branch, 0)

    def delete_branch(self, target, branch):
        self.deleted_branches.append((target.repo, branch))


class FakeSessions:
    def __init__(self, alive=(), idle=None, tail="…pane tail…",
                 resume_raises=(), spawn_raises=()):
        self.alive_set = set(alive)
        self.idle = dict(idle or {})
        self.idle_queried = []
        self.tail = tail
        self.spawned, self.resumed, self.ended = [], [], []
        self.sent_text = []
        # Issues whose launch explodes the way a vanished worktree does:
        # containers.clone_root reads <worktree>/.git to find the clone to
        # mount, so a swept or half-removed checkout raises here.
        self.resume_raises = set(resume_raises)
        self.spawn_raises = set(spawn_raises)

    def is_alive(self, issue):
        return issue in self.alive_set

    def idle_seconds(self, issue):
        self.idle_queried.append(issue)
        return self.idle.get(issue)

    def send_text(self, issue, text):
        self.sent_text.append((issue, text))

    def spawn_stage(self, issue, worktree, prompt, stage_name, model):
        if issue in self.spawn_raises:
            raise FileNotFoundError(
                f"[Errno 2] No such file or directory: '{worktree}/.git'")
        self.spawned.append((issue, stage_name, model, prompt))

    def resume(self, issue, worktree, message, model):
        if issue in self.resume_raises:
            raise FileNotFoundError(
                f"[Errno 2] No such file or directory: '{worktree}/.git'")
        self.resumed.append((issue, message, model))

    def capture_tail(self, issue, lines=25):
        return self.tail

    def end(self, issue):
        self.ended.append(issue)


class FakeNotifier:
    def __init__(self, msg_id=77):
        self.msg_id = msg_id  # telegram/notify.Notifier returns 0 on send failure
        self.sent = []
        self.contexts = []
        self.calls = []  # (template, ctx) — parallel record; sent stays template-only

    def send(self, template, **ctx):
        self.sent.append(template)
        self.contexts.append((template, ctx))
        self.calls.append((template, ctx))
        return self.msg_id


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


def deps(gh=None, sess=None, notifier=None):
    return main.Deps(github=gh or FakeGitHub(),
                     sessions=sess or FakeSessions(),
                     notifier=notifier or FakeNotifier())


def make_task(c, issue=42, stage=Stage.IMPLEMENT, slot=0,
              updated_at="2026-07-21T00:00:00+00:00", **kw):
    wt = Path(c.targets[0].worktrees_path) / f"task-{issue}"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    ts = TaskState(issue=issue, target="portfolio_eval", stage=stage, slot=slot,
                   worktree=str(wt), branch=f"agent/task-{issue}", title="t",
                   updated_at=updated_at, **kw)
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
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "claude-opus-4-8")]
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


SPEC_URL = ("https://github.com/jesdi/portfolio_eval/blob/agent/task-42/"
            "docs/superpowers/specs/x-design.md")


def _awaiting_review_task(c, wt):
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-review", "note": "spec ready",
         "artifact": "docs/superpowers/specs/x-design.md"}))


def test_awaiting_review_publishes_comments_and_links(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC)
    _awaiting_review_task(c, wt)
    seen = []
    monkeypatch.setattr(main.spec_publish, "ensure_published",
                        lambda **kw: (seen.append(kw)
                                      or spec_publish.PublishResult(url=SPEC_URL)))
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive={42}))
    main.run_pass(c, d)
    # backstop called with the task's real coordinates
    assert seen[0]["branch"] == "agent/task-42"
    assert seen[0]["repo"] == "jesdi/portfolio_eval"
    assert seen[0]["artifact"] == "docs/superpowers/specs/x-design.md"
    # one-tap link on the issue…
    assert gh.comments == [(42, f"📝 Spec ready for review: {SPEC_URL}")]
    # …and in the Telegram ping
    (tmpl, ctx), = [x for x in d.notifier.contexts
                    if x[0] == "awaiting_spec_review"]
    assert f"spec: {SPEC_URL}" in ctx["note"]
    assert ctx["note"].startswith("spec ready")


def test_publish_failure_says_local_only_and_skips_comment(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC)
    _awaiting_review_task(c, wt)
    monkeypatch.setattr(
        main.spec_publish, "ensure_published",
        lambda **kw: spec_publish.PublishResult(error="git push failed: auth"))
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive={42}))
    main.run_pass(c, d)
    assert gh.comments == []
    # the gate itself is NOT blocked by the failure
    assert load(c.state_dir, 42).stage is Stage.AWAITING_SPEC_REVIEW
    (tmpl, ctx), = [x for x in d.notifier.contexts
                    if x[0] == "awaiting_spec_review"]
    assert "⚠️ spec is local only: git push failed: auth" in ctx["note"]


def test_comment_failure_is_best_effort(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC)
    _awaiting_review_task(c, wt)
    monkeypatch.setattr(main.spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url=SPEC_URL))
    gh = FakeGitHub(); gh.comment_raises = True
    d = deps(gh, FakeSessions(alive={42}))
    main.run_pass(c, d)  # must not raise
    assert load(c.state_dir, 42).stage is Stage.AWAITING_SPEC_REVIEW
    assert any(t == "awaiting_spec_review" for t in d.notifier.sent)
    (tmpl, ctx), = [x for x in d.notifier.contexts
                    if x[0] == "awaiting_spec_review"]
    assert f"spec: {SPEC_URL}" in ctx["note"]


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
    assert [(s[0], s[1]) for s in sess.spawned] == [(42, "spec")]
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
    assert [s[:3] for s in sess.spawned] == [(42, "plan", "claude-opus-4-8")]
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
    from dispatcher import messages
    # Message may be delivered already if resumed same pass; all_messages captures both.
    assert "run 4242 concluded: failure" in messages.all_messages(
        c.state_dir, 42)[0].text


def test_woken_task_resumes_before_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    wt = make_task(c, park=PARK_WAKE)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert sess.resumed == [(42, "Continue.", "claude-opus-4-8")]
    assert gh.claimed == []  # head-of-queue: resume consumed the only slot
    t = load(c.state_dir, 42)
    assert t.park == "" and t.park_msg_id == 0
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
    assert t.park == PARK_WAKE
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, 42)[0].text == "use oauth"


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


def test_attach_command_queues_no_message(tmp_path, monkeypatch):
    """/attach is a wake, not a message. The empty text it hands _wake must
    not land in the queue: a zero-length message shows a phantom ✉ badge on
    the card, an empty row in the thread, and appends a junk "Operator
    messages" block with an empty entry to the resume prompt."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43)  # the running task holds the only capacity unit
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    from dispatcher import messages
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE and t.hold_for_attach is True
    assert messages.all_messages(c.state_dir, 42) == []


def test_queue_message_drops_blank_text_only(tmp_path):
    c = cfg(tmp_path)
    from dispatcher import messages
    main._queue_message(c, 42, "", "dispatcher")
    main._queue_message(c, 42, "   \n ", "dispatcher")
    main._queue_message(c, 42, "use the staging URL", "jesdi@github")
    assert [m.text for m in messages.all_messages(c.state_dir, 42)] == [
        "use the staging URL"]


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

    def boom(cfg_, deps_, dry_run=False, config_path="targets.yaml"):
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


def test_broken_worktree_fails_that_task_and_pass_survives(tmp_path, monkeypatch):
    """A woken task whose worktree vanished must not take the pass with it.

    The box wedged this way for hours: task-194's checkout was gone, so
    clone_root raised inside sessions.resume, guarded_pass re-raised, and
    _resume_woken never reached the tasks behind it — no claims, no spawns,
    nothing, every pass, until a human looked."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE, slot=0,
              updated_at="2026-07-21T00:00:00+00:00")
    make_task(c, issue=43, park=PARK_WAKE, slot=1,
              updated_at="2026-07-22T00:00:00+00:00")
    gh = FakeGitHub()
    sess = FakeSessions(resume_raises=[42])
    d = deps(gh, sess)

    main.run_pass(c, d)  # must not raise

    # The task behind the broken one still got its turn.
    assert [r[0] for r in sess.resumed] == [43]
    assert load(c.state_dir, 43).park == ""
    # The broken one is failed, unparked and off the board's live columns.
    broken = load(c.state_dir, 42)
    assert broken.stage is Stage.FAILED and broken.park == ""
    assert (42, "task crashed mid-pass") in gh.released
    # Filed on the infra repo: a vanished worktree is a box-side problem
    # no session container can fix.
    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops"
    assert "task-crash" in title and "#42" in title
    assert "No such file or directory" in body
    assert "task_failed" in d.notifier.sent


def test_broken_worktree_task_crash_dedupes_to_one_issue(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE, slot=0)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(resume_raises=[42]))
    main.run_pass(c, d)
    # Second pass: the task is FAILED now, so it is not woken again — but a
    # re-park by the operator must not file a second identical issue.
    save(c.state_dir, dc_replace(load(c.state_dir, 42),
                                 stage=Stage.IMPLEMENT, park=PARK_WAKE))
    main.run_pass(c, d)
    assert len(gh.created_issues) == 1
    assert d.notifier.sent.count("task_failed") == 1


def test_broken_worktree_on_spawn_fails_that_task_and_pass_survives(
        tmp_path, monkeypatch):
    """Same isolation on the spawn path: _drive_task advancing a stage into
    a vanished worktree fails the task, not the pass."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    # Gate stage with a dead session: _drive_task respawns it in place.
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=0)
    gh = FakeGitHub()
    sess = FakeSessions(spawn_raises=[42])
    d = deps(gh, sess)

    main.run_pass(c, d)  # must not raise

    assert sess.spawned == []
    assert load(c.state_dir, 42).stage is Stage.FAILED
    assert "task-crash" in gh.created_issues[0][1]


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
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "claude-sonnet-4-6")]


def test_unmatched_task_spawns_on_the_default_model(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=None, labels=("auto",))])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "claude-opus-4-8")]


def test_frontend_task_spawns_spec_on_fable(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=3,
                               labels=("auto", "frontend"))])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "claude-fable-5")]


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
    assert [s[:3] for s in sess.spawned] == [(42, "plan", "claude-fable-5")]

    # …and the implement stage of the same task drops to opus
    t = load(c.state_dir, 42)
    assert main._model_for(c, c.targets[0], t, Stage.IMPLEMENT) == "claude-opus-4-8"


def test_resume_uses_the_model_for_the_parked_stage(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT, park=PARK_WAKE,
              effort=1, labels=("auto",))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [(42, "Continue.", "claude-sonnet-4-6")]


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
              effort=3, labels=("auto", "frontend"))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [(42, "Continue.", "claude-fable-5")]


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


def test_status_counts_the_slot_the_triage_sweep_holds(tmp_path, monkeypatch):
    """The sweep's work is a tmux session, not a TaskState, so active() cannot
    see it — but _run_pass reduces effective capacity for it. Without counting
    it here, /status reports 'capacity 1/2' on a full box for the whole sweep,
    which is the operator's primary visibility surface lying."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, target="portfolio_eval", stage=Stage.IMPLEMENT, slot=0,
        worktree="/wt", branch="agent/task-42", title="Fix rounding",
        updated_at="2026-07-24T00:00:00+00:00"))
    assert main._status_lines(c)[-1] == f"capacity 1/{c.capacity}"
    monkeypatch.setattr(main.triage, "running", lambda: True)
    lines = main._status_lines(c)
    assert lines[-1] == f"capacity 2/{c.capacity}"
    assert "triage sweep running (holds 1 slot)" in lines


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
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "claude-fable-5")]


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
    make_task(c, park=PARK_WAKE)
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
    assert t.park == PARK_WAKE
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, 42)[0].text == "use oauth"
    assert intents_mod.list_intents(c.state_dir) == []
    applied = [e for e in eventlog.read_tail(c.state_dir)
               if e["event"] == "intent-applied"]
    assert applied[0]["actor"] == "jesdi@github"
    assert applied[0]["detail"] == "reply" and applied[0]["issue"] == 42


def test_reply_intent_for_running_task_is_queued_but_task_stays_running(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)  # not parked — session is live
    intents_mod.write_intent(c.state_dir, "reply", 42, {"text": "hi"}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    # message is queued — not dropped — and the task stays unparked
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, 42)[0].text == "hi"
    assert load(c.state_dir, 42).park == ""  # no wake flip for a live session
    assert intents_mod.list_intents(c.state_dir) == []  # intent deleted


def test_reply_intent_on_gate_parked_task_wakes_it(tmp_path, monkeypatch):
    # The console SpecPanel "approve" button sends a `reply` intent; the task
    # is PARK_REVIEW (not PARK_HUMAN), so _apply_one_intent must accept it.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_REVIEW, park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot → 42 stays PARK_WAKE
    intents_mod.write_intent(c.state_dir, "reply", 42,
                             {"text": "Approved — proceed."}, actor="jesdi@github",
                             epoch_ms=1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, 42)[0].text == "Approved — proceed."
    assert intents_mod.list_intents(c.state_dir) == []


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


def test_cancel_intent_ends_session_moves_board_and_tombstones(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    mark_waiting(c.state_dir, 42)
    intents_mod.write_intent(c.state_dir, "cancel", 42, {}, "op", 1)
    gh = FakeGitHub()
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(gh, sess))
    assert sess.ended == [42]
    assert gh.canceled == [42]
    assert load(c.state_dir, 42).stage is Stage.CANCELED
    assert not has_waiting(c.state_dir, 42)
    ev = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "canceled"]
    assert ev[0]["detail"] == "canceled by operator"


def test_cancel_intent_survives_github_failure(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)

    class CancelFailsGitHub(FakeGitHub):
        def cancel(self, target, issue):
            raise RuntimeError("gh outage")

    intents_mod.write_intent(c.state_dir, "cancel", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(CancelFailsGitHub(), sess))  # must not raise
    assert sess.ended == [42]
    assert load(c.state_dir, 42).stage is Stage.CANCELED


def test_cancel_intent_does_not_reclaim_issue_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents_mod.write_intent(c.state_dir, "cancel", 42, {}, "op", 1)
    # The board write can lag (or fail): #42 may still rank as a candidate
    # this pass; the CANCELED tombstone must stop _claim_new.
    gh = FakeGitHub([Candidate(42, "Canceled but still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions(alive={42})))
    assert gh.claimed == []
    assert load(c.state_dir, 42).stage is Stage.CANCELED


def test_cancel_intent_without_task_file_uses_payload_target(tmp_path, monkeypatch):
    # A backlog card has no task file; the web UI names the target in the
    # payload so the board/issue side still happens. No tombstone is written.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    intents_mod.write_intent(c.state_dir, "cancel", 99,
                             {"target": "portfolio_eval"}, "op", 1)
    gh = FakeGitHub()
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.canceled == [99]
    assert load(c.state_dir, 99) is None


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
    from dispatcher import messages
    # Task 3: _resume_woken delivers the queued message into the resume prompt.
    msgs = messages.all_messages(c.state_dir, 42)
    assert msgs[0].text == "The operator resumed this task. Continue."
    assert msgs[0].delivered_at != ""
    assert "The operator resumed this task. Continue." in sess.resumed[0][1]


def test_resume_intent_carries_optional_text(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    intents_mod.write_intent(c.state_dir, "resume", 42, {"text": "ship it"},
                             "op", 1)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    # Task 3: _resume_woken delivers the queued message into the resume prompt.
    msgs = messages.all_messages(c.state_dir, 42)
    assert msgs[0].text == "ship it"
    assert msgs[0].delivered_at != ""
    assert "ship it" in sess.resumed[0][1]


def test_failed_intent_does_not_abort_pass_or_remaining_intents(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 2)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43, park=PARK_HUMAN, park_msg_id=56)
    make_task(c, issue=44)  # active; keeps 42/43 at PARK_WAKE... capacity 2 → one resumes

    real_wake = main._wake

    def wake_or_boom(cfg_, task, text, hold=False, actor="dispatcher"):
        if task.issue == 42:
            raise RuntimeError("disk full")
        real_wake(cfg_, task, text, hold=hold, actor=actor)

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
    make_task(c, issue=42, park=PARK_WAKE)
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
    make_task(c, issue=42, park=PARK_WAKE)
    mark_attached(c.state_dir, 42)
    sess = FakeSessions()
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert sess.resumed == []
    clear_attached(c.state_dir, 42)
    main.run_pass(c, d)
    assert sess.resumed == [(42, "Continue.", "claude-opus-4-8")]


def test_stalled_working_session_parks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "working"}))
    sess = FakeSessions(alive=[42], idle={42: 999999.0})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == PARK_HUMAN
    assert sess.ended == [42]
    assert "parked_question" in d.notifier.sent


def test_no_signal_stalled_session_parks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)  # no stage.json written
    sess = FakeSessions(alive=[42], idle={42: 999999.0})
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, 42).park == PARK_HUMAN


def test_stall_disabled_never_queries_idle(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), stall_after_seconds=0)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "working"}))
    sess = FakeSessions(alive=[42], idle={42: 999999.0})
    main.run_pass(c, deps(sess=sess))
    assert sess.idle_queried == []
    assert load(c.state_dir, 42).park == ""


def test_dead_session_never_queries_idle(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT)
    sess = FakeSessions(alive=[], idle={42: 999999.0})
    main.run_pass(c, deps(sess=sess))
    assert sess.idle_queried == []


LOGIN_TAIL = ("Browser didn't open? Use the url below to sign in:\n"
              "https://claude.ai/oauth/authorize?code=true&client_id=abc\n"
              "Paste code here if prompted >")


def test_stall_with_login_tail_parks_login_and_keeps_session(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)  # no stage.json — startup login prompt
    sess = FakeSessions(alive=[42], idle={42: 999999.0}, tail=LOGIN_TAIL)
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == PARK_LOGIN
    assert t.park_msg_id == 77          # FakeNotifier.send returns 77
    assert sess.ended == []             # pane must stay ALIVE
    assert d.notifier.sent == ["needs_relogin"]
    _, ctx = d.notifier.calls[0]
    assert ctx["login_url"] == ("https://claude.ai/oauth/authorize"
                                "?code=true&client_id=abc")
    assert LOGIN_TAIL.splitlines()[0] in ctx["note"]


def test_blocked_park_with_login_tail_also_classifies(tmp_path, monkeypatch):
    # classification happens at park time regardless of what triggered it
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "blocked", "note": "x"}))
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, 42).park == PARK_LOGIN
    assert sess.ended == []


def test_stall_without_login_tail_stays_generic(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)
    sess = FakeSessions(alive=[42], idle={42: 999999.0})  # default tail
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert load(c.state_dir, 42).park == PARK_HUMAN
    assert sess.ended == [42]
    assert "parked_question" in d.notifier.sent


def test_login_park_when_ping_fails_falls_back_to_generic_park(tmp_path,
                                                               monkeypatch):
    # msg_id 0 = the Telegram send failed, so no reply can ever reach this
    # task. Holding a live container for an unreachable park is worse than a
    # plain park: release it.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)
    sess = FakeSessions(alive=[42], idle={42: 999999.0}, tail=LOGIN_TAIL)
    d = deps(sess=sess, notifier=FakeNotifier(msg_id=0))
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == PARK_HUMAN
    assert sess.ended == [42]           # container released
    assert d.notifier.sent == ["needs_relogin", "parked_question"]


def test_login_park_holds_capacity_against_new_claims(tmp_path, monkeypatch):
    # A login park keeps its container running, so it must NOT free a slot
    # for a new claim — a box-wide auth expiry would otherwise spawn a fresh
    # doomed session on every pass.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, stage=Stage.SPEC, park=PARK_LOGIN, park_msg_id=77)
    gh = FakeGitHub([Candidate(99, "X", "u")])
    main.run_pass(c, deps(gh, FakeSessions(alive=[42], tail=LOGIN_TAIL)))
    assert gh.claimed == []


def test_resume_woken_ends_session_before_resuming(tmp_path, monkeypatch):
    # /attach on a login park reaches resume with the pane still LIVE;
    # _launch would type the podman command into the running claude.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT, park=PARK_WAKE)
    sess = FakeSessions(alive=[42])
    main.run_pass(c, deps(sess=sess))
    assert sess.ended == [42]
    assert sess.resumed == [(42, "Continue.", "claude-opus-4-8")]


def test_reply_to_login_park_injects_code(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, park=PARK_LOGIN, park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77,
                                               text="oauth-code-abc#123")])
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    assert sess.sent_text == [(42, "oauth-code-abc#123")]
    assert sess.resumed == []          # must NOT go through resume
    t = load(c.state_dir, 42)
    assert t.park == "" and t.park_msg_id == 0


def test_reply_to_human_park_still_wakes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT, park=PARK_HUMAN, park_msg_id=88)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=88, text="hi")])
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, 42)
    assert sess.sent_text == []
    # _wake marks PARK_WAKE and queues the text; _resume_woken delivers it into the prompt.
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, 42) == []
    assert messages.all_messages(c.state_dir, 42)[0].text == "hi"
    assert sess.resumed and "hi" in sess.resumed[0][1]


def test_unmatched_reply_still_reports(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, park=PARK_LOGIN, park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=999, text="x")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent
    assert load(c.state_dir, 42).park == PARK_LOGIN  # untouched


def test_injection_refused_when_pane_left_the_login_prompt(tmp_path, monkeypatch):
    # The pane is a HOST tmux session: once claude exits, `has-session` still
    # says alive and send_text would run the operator's code as a shell
    # command outside the sandbox.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, park=PARK_LOGIN, park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77, text="code")])
    sess = FakeSessions(alive=[42], tail="agent@box:~$ ")
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert sess.sent_text == []
    assert load(c.state_dir, 42).park == PARK_LOGIN  # park kept, not cleared
    assert "status" in d.notifier.sent


def test_injection_restores_the_unpark_invariant(tmp_path, monkeypatch):
    # Same invariant _resume_woken/_retry_plan enforce: a stale blocked signal
    # or waiting marker would re-park (and END) the freshly re-authed session.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT, park=PARK_LOGIN, park_msg_id=77)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "x"}))
    mark_waiting(c.state_dir, 42)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77, text="code")])
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, 42)
    assert t.park == ""
    assert not has_waiting(c.state_dir, 42)
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"
    assert sess.ended == []             # NOT re-parked over the live session


def test_login_park_clears_the_waiting_marker(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "working"}))
    mark_waiting(c.state_dir, 42)
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, 42).park == PARK_LOGIN
    assert not has_waiting(c.state_dir, 42)


def test_injection_logs_a_distinct_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, park=PARK_LOGIN, park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=77, text="code")])
    main.run_pass(c, deps(sess=FakeSessions(alive=[42], tail=LOGIN_TAIL)))
    events = [e["event"] for e in eventlog.read_tail(c.state_dir)]
    assert "login-code-injected" in events
    assert "resumed" not in events


def gate_signal(wt: Path, artifact: str = "spec.md") -> None:
    """The spec session's 'draft ready, review it' signal."""
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-review", "note": "spec ready",
         "artifact": artifact}))


def test_gate_park_ends_session_and_frees_capacity_and_slot(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=1)
    gate_signal(wt)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == PARK_REVIEW
    assert t.slot == NO_SLOT
    assert t.stage is Stage.AWAITING_SPEC_REVIEW   # stage preserved
    assert t.park_msg_id == 77                     # reply-to-wake target
    assert sess.ended == [42]
    assert "spec_parked" in d.notifier.sent


def test_gate_park_is_event_logged(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW)
    gate_signal(wt)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    parked = [e for e in eventlog.read_tail(c.state_dir)
              if e["event"] == "parked"]
    assert len(parked) == 1
    assert parked[0]["issue"] == 42
    assert parked[0]["stage"] == "awaiting-spec-review"
    assert parked[0]["detail"] == "spec review grace expired"


def test_gate_holds_inside_the_grace_period(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW,
                   updated_at=fresh)
    gate_signal(wt)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.park == "" and t.slot == 0
    assert sess.ended == []
    assert "spec_parked" not in d.notifier.sent


def test_attached_operator_suspends_the_grace_timer(tmp_path, monkeypatch):
    # A human reviewing in the live session must never be parked out from
    # under; has_attached blocks _drive_task entirely.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW)
    gate_signal(wt)
    mark_attached(c.state_dir, 42)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, 42).park == ""
    assert sess.ended == []


def test_gate_parked_task_does_not_block_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_REVIEW, park_msg_id=77)
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [99]


def test_zero_grace_parks_on_the_next_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW,
                   updated_at=datetime.now(timezone.utc).isoformat())
    gate_signal(wt)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, 42).park == PARK_REVIEW


def test_unparseable_timestamp_never_expires(tmp_path, monkeypatch):
    # Fail closed: a corrupt updated_at must not silently park everything.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW,
                   updated_at="not-a-timestamp")
    gate_signal(wt)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, 42).park == ""


def test_woken_gate_parked_task_gets_a_fresh_slot(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_WAKE)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, 42)
    assert t.park == ""
    assert t.slot in range(3)   # a real slot, not NO_SLOT
    assert sess.resumed == [(42, "Continue.", "claude-opus-4-8")]


def test_woken_gate_parked_task_waits_when_every_slot_is_taken(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    # capacity=1 → max_slots=3; filling all 3 slots also exhausts capacity
    # (holds_slot and consumes_capacity are the same predicate, so slot
    # exhaustion always implies capacity exhaustion with the new formula).
    c = dc_replace(cfg(tmp_path), capacity=1)
    for issue, slot in ((1, 0), (2, 1), (3, 2)):
        make_task(c, issue=issue, stage=Stage.IMPLEMENT, slot=slot)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_WAKE)
    sess = FakeSessions(alive={1, 2, 3})
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE and t.slot == NO_SLOT  # still parked, retried next pass
    assert 42 not in [issue for issue, _msg, _model in sess.resumed]


def test_slot_less_back_pressure_does_not_starve_other_woken_tasks(tmp_path, monkeypatch):
    # Skipping a slot-less task must not abandon the rest of the wake queue:
    # a task that already holds a slot resumes in the same pass.
    # Issues 1 and 2 occupy slots 0 and 1; issue 43 already holds slot 2 and
    # sits second in the wake queue, behind slot-less issue 42. The invariant
    # under test is the `continue` in _resume_woken: whatever the head of the
    # queue does, the rest of it is still processed in the same pass.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=9)
    for issue, slot in ((1, 0), (2, 1)):
        make_task(c, issue=issue, stage=Stage.IMPLEMENT, slot=slot)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_WAKE, updated_at="2026-07-21T00:00:00+00:00")
    make_task(c, issue=43, stage=Stage.IMPLEMENT, slot=2, park=PARK_WAKE,
              updated_at="2026-07-21T00:00:01+00:00")
    sess = FakeSessions(alive={1, 2})
    main.run_pass(c, deps(sess=sess))
    assert 43 in [issue for issue, _msg, _model in sess.resumed]


def test_spec_parked_ping_links_spec(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, stage=Stage.AWAITING_SPEC_REVIEW,
                   artifact="docs/superpowers/specs/x-design.md")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-review", "note": "ready",
         "artifact": "docs/superpowers/specs/x-design.md"}))
    monkeypatch.setattr(main.spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url=SPEC_URL))
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    (tmpl, ctx), = [x for x in d.notifier.contexts if x[0] == "spec_parked"]
    assert f"spec: {SPEC_URL}" in ctx["note"]


def test_spec_parked_note_says_local_only_when_publish_fails(
        tmp_path, monkeypatch):
    """_park_for_review must re-run ensure_published and surface its failure
    in the morning ping so the operator knows the link is broken, not silently
    embed a 404."""
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, stage=Stage.AWAITING_SPEC_REVIEW,
                   artifact="docs/superpowers/specs/x-design.md")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-review", "note": "ready",
         "artifact": "docs/superpowers/specs/x-design.md"}))
    monkeypatch.setattr(
        main.spec_publish, "ensure_published",
        lambda **kw: spec_publish.PublishResult(error="git push failed: auth"))
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    (tmpl, ctx), = [x for x in d.notifier.contexts if x[0] == "spec_parked"]
    assert "⚠️ spec is local only: git push failed: auth" in ctx["note"]
    # No URL anywhere — the operator must not see a link that 404s.
    assert "https://github.com" not in ctx["note"]


def test_spec_error_redacts_tokenized_url_in_note(tmp_path, monkeypatch):
    """A tokenized remote URL in push stderr must not reach the Telegram note."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-review", "note": "ready",
         "artifact": "docs/specs/x-design.md"}))
    token_error = (
        "git push failed: fatal: unable to access "
        "'https://x-access-token:ghp_SECRET@github.com/jesdi/r.git/': "
        "The requested URL returned error: 403"
    )
    monkeypatch.setattr(
        main.spec_publish, "ensure_published",
        lambda **kw: spec_publish.PublishResult(error=token_error))
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    (tmpl, ctx), = [x for x in d.notifier.contexts
                    if x[0] == "awaiting_spec_review"]
    assert "ghp_SECRET" not in ctx["note"]
    assert "⚠️ spec is local only:" in ctx["note"]


def test_reply_to_the_spec_parked_message_wakes_the_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_REVIEW, park_msg_id=55)
    make_task(c, issue=43)  # active task consumes the only capacity unit
    patch_events(monkeypatch, [Reply(reply_to_msg_id=55,
                                     text="drop the caching section")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, 42)[0].text == "drop the caching section"


def test_plain_text_wakes_a_single_gate_parked_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_REVIEW, park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Plain(text="ok")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, 42)[0].text == "ok"


def test_plain_text_asks_which_when_a_human_park_and_a_gate_park_coexist(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_REVIEW, park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, 42).park == PARK_HUMAN
    assert load(c.state_dir, 43).park == PARK_REVIEW
    assert "status" in d.notifier.sent  # the "Which task?" prompt lists both


def test_two_slot_less_woken_tasks_get_distinct_slots(tmp_path, monkeypatch):
    # Regression: two PARK_WAKE tasks with slot=NO_SLOT must not collide —
    # _resume_woken re-reads load_all per iteration and saves each new slot
    # before the next iteration allocates, so both should get distinct slots.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=9)
    make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_WAKE,
              updated_at="2026-07-21T00:00:00+00:00")
    make_task(c, issue=43, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
              park=PARK_WAKE,
              updated_at="2026-07-21T00:00:01+00:00")
    main.run_pass(c, deps(sess=FakeSessions()))
    t42 = load(c.state_dir, 42)
    t43 = load(c.state_dir, 43)
    assert t42.slot in range(3)
    assert t43.slot in range(3)
    assert t42.slot != t43.slot


def test_status_line_says_no_slot_for_a_gate_parked_task(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, target="portfolio_eval", stage=Stage.AWAITING_SPEC_REVIEW,
        slot=NO_SLOT, worktree="/wt", branch="agent/task-42", title="Add widget",
        updated_at="2026-07-28T00:00:00+00:00", park=PARK_REVIEW,
        effort=1, labels=("auto",)))
    lines = main._status_lines(c)
    assert lines[0].endswith("[awaiting-review] (no slot)")
    assert "(slot -1)" not in lines[0]
    assert lines[-1] == "capacity 0/3"   # gate-parked holds no capacity


class LiveUntilEnded(FakeSessions):
    """A session is alive from spawn/resume until the dispatcher ends it —
    the property the gate-park path depends on (ending the session is what
    frees capacity), and the one a fixed `FakeSessions(alive=...)` set cannot
    express across a dozen passes."""

    def spawn_stage(self, issue, worktree, prompt, stage_name, model):
        super().spawn_stage(issue, worktree, prompt, stage_name, model)
        self.alive_set.add(issue)

    def resume(self, issue, worktree, message, model):
        super().resume(issue, worktree, message, model)
        self.alive_set.add(issue)

    def end(self, issue):
        super().end(issue)
        self.alive_set.discard(issue)


def test_overnight_drain_parks_every_ready_spec_then_one_reply_advances_one(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=2, spec_review_grace_minutes=0)
    gh = FakeGitHub([Candidate(n, f"task {n}", f"u{n}") for n in range(1, 6)])
    sess = LiveUntilEnded()
    d = deps(gh, sess)

    # Each pass: every spec session that has been spawned reports its draft
    # ready, then the dispatcher runs. With grace 0 a task parks on the pass
    # after its stage flips to the gate, freeing capacity AND its slot for
    # the next Ready candidate.
    for _ in range(12):
        for t in load_all(c.state_dir):
            if t.stage in (Stage.SPEC, Stage.AWAITING_SPEC_REVIEW) and not t.park:
                gate_signal(Path(t.worktree), artifact="spec.md")
        main.run_pass(c, d)

    tasks = load_all(c.state_dir)
    assert sorted(t.issue for t in tasks) == [1, 2, 3, 4, 5]
    assert all(t.park == PARK_REVIEW for t in tasks), \
        [(t.issue, t.stage.value, t.park) for t in tasks]
    assert all(t.slot == NO_SLOT for t in tasks)
    assert sorted(gh.claimed) == [1, 2, 3, 4, 5]   # capacity 2 did NOT cap it

    # Morning: one reply resumes exactly one task, onto a real slot, and it
    # advances to PLAN once the session signals the approved spec is done.
    patch_events(monkeypatch, [Reply(reply_to_msg_id=77, text="ship it")])
    main.run_pass(c, d)
    resumed = [t for t in load_all(c.state_dir) if t.park == ""]
    assert len(resumed) == 1
    woken = resumed[0]
    assert woken.slot in range(3)

    valid_spec(Path(woken.worktree))
    (Path(woken.worktree) / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md"}))
    patch_events(monkeypatch, [])
    main.run_pass(c, d)
    assert load(c.state_dir, woken.issue).stage is Stage.PLAN
    assert (woken.issue, "plan") in [(s[0], s[1]) for s in sess.spawned]


def test_park_for_input_saves_park_note(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "need a decision"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, 42).park_note == "need a decision"


def test_park_for_review_saves_park_note(tmp_path, monkeypatch):
    # Same arrangement as test_gate_park_ends_session_and_frees_capacity_and_slot
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.AWAITING_SPEC_REVIEW, slot=1)
    gate_signal(wt)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, 42).park_note == "spec ready for review"


def test_resume_clears_park_note(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, park=PARK_WAKE, park_note="stale question")
    main.run_pass(c, deps(sess=FakeSessions()))
    assert load(c.state_dir, 42).park_note == ""


def test_prune_snapshots_removes_only_old_finished(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    # old + finished (PR_OPEN is not in IN_FLIGHT_STAGES) -> pruned
    make_task(c, issue=41, stage=Stage.PR_OPEN)
    (snaps / "task-41.txt").write_text("done long ago")
    os.utime(snaps / "task-41.txt", (old, old))
    # old + absent state file -> pruned
    (snaps / "task-40.txt").write_text("state gone")
    os.utime(snaps / "task-40.txt", (old, old))
    # old + still in flight (parked implement) -> kept
    make_task(c, issue=42, park="parked")
    (snaps / "task-42.txt").write_text("still parked")
    os.utime(snaps / "task-42.txt", (old, old))
    # fresh + finished -> kept (within the 7-day window)
    make_task(c, issue=43, stage=Stage.FAILED)
    (snaps / "task-43.txt").write_text("just failed")

    main._prune_snapshots(c)

    assert not (snaps / "task-41.txt").exists()
    assert not (snaps / "task-40.txt").exists()
    assert (snaps / "task-42.txt").exists()
    assert (snaps / "task-43.txt").exists()


def test_prune_snapshots_corrupt_state_file_does_not_abort_sweep(tmp_path, monkeypatch):
    """A corrupt state file must not raise out of _prune_snapshots; the sweep
    continues and still prunes any other eligible snapshots."""
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    # corrupt state file — write invalid JSON directly
    Path(c.state_dir, "task-50.json").write_text("{not json")
    (snaps / "task-50.txt").write_text("corrupt task snap")
    os.utime(snaps / "task-50.txt", (old, old))
    # separate old+finished snapshot in the same dir (absent state file) -> must still be pruned
    (snaps / "task-51.txt").write_text("old finished")
    os.utime(snaps / "task-51.txt", (old, old))

    # must not raise
    main._prune_snapshots(c)

    # sweep continued past the corrupt file and pruned the eligible one
    assert not (snaps / "task-51.txt").exists()


# ---------------------------------------------------------------------------
# PR lifecycle tests (Task 8)
# ---------------------------------------------------------------------------

def payload(state="OPEN", merged_at=None, reviews=(), comments=()):
    return {"state": state, "mergedAt": merged_at, "reviewDecision": "",
            "reviews": list(reviews), "comments": list(comments)}


def patch_teardown(monkeypatch):
    """Records (wt, branch, dry_run) — dry_run included so the dry-run
    threading is asserted on what the callee actually received, not merely
    on the pass surviving."""
    removed = []
    monkeypatch.setattr(
        main, "remove_workspace",
        lambda target, wt, branch, dry_run=False: removed.append(
            (wt, branch, dry_run)))
    return removed


def pr_open_task(c, issue=42, pr_number=12, **kw):
    make_task(c, issue=issue, stage=Stage.PR_OPEN, slot=NO_SLOT,
              pr_number=pr_number, **kw)
    return load(c.state_dir, issue)


def test_merged_pr_moves_task_to_done(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    base = cfg(tmp_path)
    c = dc_replace(base, targets=[dc_replace(
        base.targets[0], status_done_option_id="D0")])
    t = pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    notif = FakeNotifier()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess=sess, notifier=notif))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.DONE and got.done_at
    assert (42, "D0") in gh.statused
    assert 42 in sess.ended
    assert removed == [(t.worktree, t.branch, False)]
    assert (c.targets[0].repo, t.branch) in gh.deleted_branches
    assert "task_done" in notif.sent


def test_merged_without_done_option_id_still_completes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)  # status_done_option_id == ""
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))
    assert load(c.state_dir, 42).stage is Stage.DONE
    assert gh.statused == []


def test_closed_unmerged_pr_fails_task_preserving_worktree(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    notif = FakeNotifier()
    sess = FakeSessions(alive=(42,))
    main.run_pass(c, deps(gh, sess, notifier=notif))
    assert load(c.state_dir, 42).stage is Stage.FAILED
    assert removed == [] and "pr_closed" in notif.sent
    # The implement session is still alive at pr-open and nothing will ever
    # drive a FAILED task again — the tmux session and its container must be
    # ended here or they hold their reservation until the box reboots.
    assert sess.ended == [42]


def test_feedback_sets_pending_flag_and_notifies_once(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 0)  # full: spawn deferred
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{
        "createdAt": "2026-07-30T09:00:00Z", "author": {"login": "alice"}}])
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))
    got = load(c.state_dir, 42)
    assert got.feedback_pending is True and got.stage is Stage.PR_OPEN
    assert got.feedback_cursor == ""  # cursor NOT advanced here; that is Task 9's job
    assert notif.sent.count("pr_feedback") == 1
    main.run_pass(c, deps(gh, notifier=notif))   # second pass: no re-notify
    assert notif.sent.count("pr_feedback") == 1


def test_own_and_bot_comments_do_not_reopen(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[
        {"createdAt": "2026-07-30T09:00:00Z", "author": {"login": "agent-bot"}},
        {"createdAt": "2026-07-30T09:01:00Z",
         "author": {"login": "github-actions[bot]"}}])
    main.run_pass(c, deps(gh))
    assert load(c.state_dir, 42).feedback_pending is False


def test_pr_number_resolved_from_branch_when_missing(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, pr_number=0)
    gh = FakeGitHub()
    gh.branch_prs["agent/task-42"] = 12
    gh.pr_payloads[12] = payload()
    main.run_pass(c, deps(gh))
    assert load(c.state_dir, 42).pr_number == 12


def test_poll_failure_leaves_task_untouched(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_view_raises = True
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN and got.feedback_pending is False


def test_finish_merged_gh_failure_leaves_task_pr_open_and_pass_survives(
        tmp_path, monkeypatch):
    """set_status raising mid-_finish_merged must be caught per-task: the pass
    continues (no exception escapes run_pass), the task stays PR_OPEN (state
    flip never reached), and the worktree was NOT removed (teardown runs after
    the raising step so it too was never reached)."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    base = cfg(tmp_path)
    c = dc_replace(base, targets=[dc_replace(
        base.targets[0], status_done_option_id="D0")])
    pr_open_task(c)

    class SetStatusFails(FakeGitHub):
        def set_status(self, target, issue, option_id):
            raise subprocess.CalledProcessError(1, ["gh"])

    gh = SetStatusFails()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))  # must not raise
    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN  # state flip never reached
    assert removed == []               # teardown never reached (board runs first)


def test_implement_done_captures_pr_number(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done",
        "note": "https://github.com/jesdi/portfolio_eval/pull/77",
        "artifact": "https://github.com/jesdi/portfolio_eval/pull/77"}))
    main.run_pass(c, deps(FakeGitHub()))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN and got.pr_number == 77


def test_flush_deletes_only_old_done_tasks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=1, stage=Stage.DONE, slot=NO_SLOT,
              done_at="2026-07-01T00:00:00+00:00")   # ancient
    fresh = datetime.now(timezone.utc).isoformat()
    make_task(c, issue=2, stage=Stage.DONE, slot=NO_SLOT, done_at=fresh)
    make_task(c, issue=3, stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=0)
    main.run_pass(c, deps(FakeGitHub()))
    assert load(c.state_dir, 1) is None
    assert load(c.state_dir, 2) is not None
    assert load(c.state_dir, 3) is not None


def test_pending_feedback_spawns_address_review_when_capacity_frees(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, feedback_pending=True)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()  # quiet now; pending flag drives the spawn
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.ADDRESS_REVIEW
    assert got.feedback_pending is False
    assert got.feedback_cursor != ""      # cursor = spawn time
    assert got.slot != NO_SLOT
    assert [s for s in sess.spawned if s[1] == "address-review"]
    assert 42 in sess.ended  # previous session ended before spawning


def test_pending_feedback_not_spawned_over_capacity(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=1, stage=Stage.IMPLEMENT, slot=0)  # eats the capacity
    pr_open_task(c, feedback_pending=True)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    sess = FakeSessions(alive=(1,))
    main.run_pass(c, deps(gh, sess))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN and got.feedback_pending is True


def test_pending_feedback_not_spawned_when_budget_low(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.99)
    c = cfg(tmp_path)
    pr_open_task(c, feedback_pending=True)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert load(c.state_dir, 42).stage is Stage.PR_OPEN
    assert sess.spawned == []


# ---------------------------------------------------------------------------
# PR lifecycle — final-review fixes
# ---------------------------------------------------------------------------

def merged_cfg(tmp_path):
    base = cfg(tmp_path)
    return dc_replace(base, targets=[dc_replace(
        base.targets[0], status_done_option_id="D0")])


def test_dry_run_pass_does_not_tear_down_a_merged_task(tmp_path, monkeypatch):
    """--dry-run must reach remove_workspace as a dry run, not as a real
    deletion: GitHubClient and Sessions already no-op under dry_run, so the
    worktree/branch removal was the one side effect that still executed for
    real while the operator watched `[dry-run] delete_branch …` scroll by."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    t = pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)
    assert removed == [(t.worktree, t.branch, True)]


def test_remove_workspace_dry_run_flag_reaches_the_real_teardown(
        tmp_path, monkeypatch, capsys):
    """Same path with the real workspace module in place: nothing shells
    out. Guards the signature itself — a remove_workspace that ignored
    dry_run would still run git here."""
    patch_usage(monkeypatch)
    from dispatcher import workspace as workspace_mod
    monkeypatch.setattr(
        workspace_mod, "_sh",
        lambda *a, **k: pytest.fail("git ran under --dry-run"))
    c = merged_cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)
    # The merged branch really was taken (so _sh had its chance to run),
    # and the task stayed put — a dry run reports the teardown, never
    # performs it and never records it as performed.
    assert "[dry-run] remove worktree" in capsys.readouterr().out
    assert load(c.state_dir, 42).stage is Stage.PR_OPEN


def test_merged_task_with_a_human_attached_is_left_alone(tmp_path, monkeypatch):
    """attached-<N> is not advisory. A teammate merging while the operator
    sits in #42's web terminal must not kill the session and delete the
    worktree out from under the live pty — skip, retry next pass (a merged
    PR still reads merged)."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    pr_open_task(c)
    mark_attached(c.state_dir, 42)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    sess = FakeSessions(alive=(42,))
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN          # still polled next pass
    assert removed == []                       # worktree intact
    assert sess.ended == []                    # pty untouched
    assert gh.statused == [] and gh.deleted_branches == []
    assert "task_done" not in notif.sent


def test_pending_feedback_not_spawned_while_a_human_is_attached(
        tmp_path, monkeypatch):
    """Feedback arriving while the operator is in the web terminal must not
    end their session and spawn address-review into the same worktree.
    feedback_pending persists on disk, so the round happens after detach."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, feedback_pending=True)
    mark_attached(c.state_dir, 42)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    sess = FakeSessions(alive=(42,))
    main.run_pass(c, deps(gh, sess))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN and got.feedback_pending is True
    assert got.feedback_cursor == ""   # cursor NOT advanced by a skipped spawn
    assert sess.spawned == [] and sess.ended == []


def test_address_review_resolves_the_implement_model(tmp_path, monkeypatch):
    """address-review writes production code and pushes it to the PR, so it
    resolves against the policy's `implement:` key — not the cheap default
    it silently fell through to."""
    patch_usage(monkeypatch)
    policy = parse_policy({
        "default": "claude-fable-5",
        "rules": [{"name": "everything", "when": {"effort": {"min": 0}},
                   "use": {"spec": "claude-fable-5",
                           "implement": "claude-opus-4-8"}}],
    })
    c = dc_replace(cfg(tmp_path), models=policy)
    pr_open_task(c, feedback_pending=True, effort=1)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] for s in sess.spawned] == [(42, "address-review", "claude-opus-4-8")]


def test_cursor_now_never_seals_the_second_it_was_taken_in():
    """GitHub stamps comments to the second, and pr_poll's trigger is a
    strict `>`. A cursor whose own second is already sealed hides every
    comment GitHub reports in that second — including ones posted after the
    session started. The cursor must be strictly below the second any
    comment written from now on can carry."""
    cursor = datetime.fromisoformat(main._cursor_now())
    now_second = datetime.now(timezone.utc).replace(microsecond=0)
    assert cursor.microsecond == 0          # no sub-second precision
    assert cursor < now_second


def test_spawned_cursor_still_sees_a_comment_from_the_spawn_second(
        tmp_path, monkeypatch):
    """End to end: spawn address-review, then post a comment stamped with
    the whole second the spawn happened in. The next poll must classify it
    as fresh feedback (a redundant round beats a lost comment)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, feedback_pending=True)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, 42)
    assert got.stage is Stage.ADDRESS_REVIEW and got.feedback_cursor

    # Derive the spawn second from the cursor the pass actually wrote.
    # _cursor_now() returns floor(now) - 1s, so the second a comment posted
    # during the spawn can carry is cursor + 1s.  Sampling the clock before
    # run_pass is racy: if the wall-clock second ticks over between the sample
    # and the _cursor_now() call, the derived stamp can be one second off.
    #
    # Non-tautology guard: assert spawn_second is not in the future.
    # If _cursor_now() stops subtracting, cursor = floor(now), making
    # spawn_second = floor(now) + 1s — a future second — and this assert fires.
    # (run_pass completes in <1 ms in tests; a 1-second gap is impossible.)
    cursor_dt = datetime.fromisoformat(got.feedback_cursor)
    spawn_second = cursor_dt + timedelta(seconds=1)
    assert spawn_second <= datetime.now(timezone.utc).replace(microsecond=0), (
        f"feedback_cursor {got.feedback_cursor!r} looks like floor(now), not "
        f"floor(now)-1s — _cursor_now() may have stopped subtracting"
    )
    stamp = spawn_second.strftime("%Y-%m-%dT%H:%M:%SZ")
    res = main.pr_poll.classify(
        payload(comments=[{"createdAt": stamp,
                           "author": {"login": "alice"}}]),
        got.feedback_cursor, "agent-bot")
    assert res.kind == "feedback"


def test_address_review_done_returns_task_to_pr_open(tmp_path, monkeypatch):
    """The return leg, at run_pass level: a task that STARTS the pass at
    address-review with a `done` signal must land back at pr-open with its
    PR number intact, and _poll_prs — which re-reads it later in the same
    pass, now against the freshly-advanced cursor — must not re-flag
    feedback for a comment older than that cursor or spawn a second round."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    cursor = "2026-07-30T09:30:00+00:00"
    wt = make_task(c, issue=42, stage=Stage.ADDRESS_REVIEW, slot=0,
                   pr_number=12, feedback_cursor=cursor)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "address-review", "status": "done",
        "note": "pushed review fixes", "artifact": ""}))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{
        "createdAt": "2026-07-30T09:00:00Z", "author": {"login": "alice"}}])
    sess = FakeSessions(alive=(42,))
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, sess, notifier=notif))

    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN
    assert got.pr_number == 12              # regex found nothing; not clobbered
    assert got.feedback_pending is False    # comment predates the cursor
    assert got.feedback_cursor == cursor    # only a spawn moves it
    assert sess.spawned == []               # no second address-review round
    assert "pr_updated" in notif.sent


# ---------------------------------------------------------------------------
# --dry-run must not touch local state
#
# Every I/O *dependency* was already stubbed under --dry-run (GitHubClient,
# Sessions, Notifier, remove_workspace), but the local writes — state files,
# events.jsonl, and the markers/caches that live beside them — ran for real.
# A dry run therefore advanced tasks to a terminal stage and wrote history
# while every visible side effect printed "[dry-run] …" and was skipped.
# ---------------------------------------------------------------------------

def test_dry_run_over_a_merged_pr_leaves_state_and_events_untouched(
        tmp_path, monkeypatch):
    """The reproduced production bug: a dry run over four merged PRs flipped
    them pr-open → done and appended four `merged` events, so the real pass
    that follows never performs the teardown (_poll_prs only sees PR_OPEN)."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    pr_open_task(c, pr_number=0)          # forces the pr_number backfill too
    gh = FakeGitHub()
    gh.branch_prs["agent/task-42"] = 12
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)

    got = load(c.state_dir, 42)
    assert got.stage is Stage.PR_OPEN
    assert got.done_at == ""
    assert got.pr_number == 0             # not even the benign backfill
    assert eventlog.read_tail(c.state_dir) == []


def test_dry_run_does_not_write_state_for_a_newly_claimed_task(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    main.run_pass(c, deps(gh), dry_run=True)

    assert load(c.state_dir, 42) is None
    assert eventlog.read_tail(c.state_dir) == []


def test_dry_run_does_not_flush_expired_done_tasks(tmp_path, monkeypatch):
    """_flush_done deletes state files outright and takes no dry_run
    parameter at all — the leak is not confined to the paths that thread
    one."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=1, stage=Stage.DONE, slot=NO_SLOT,
              done_at="2026-07-01T00:00:00+00:00")   # ancient
    main.run_pass(c, deps(FakeGitHub()), dry_run=True)

    assert load(c.state_dir, 1) is not None


def test_dry_run_leaves_the_real_state_dir_alone_entirely(
        tmp_path, monkeypatch):
    """Belt-and-braces over the whole directory: no file under state_dir may
    change during a dry run, whatever the pass decides to do."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")

    def snapshot():
        root = Path(c.state_dir)
        return {p.relative_to(root): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()
                and p.name != "convergence.lock"}

    before = snapshot()
    main.run_pass(c, deps(gh), dry_run=True)
    assert snapshot() == before


# ---------------------------------------------------------------------------
# auth-dark edge tests (Task 3)
# ---------------------------------------------------------------------------

def patch_usage_dark(monkeypatch):
    monkeypatch.setattr(
        main, "fetch_usage",
        lambda state_dir: UsageSnapshot(0.0, 0.0, "unavailable"))


def dark_marker(c):
    return Path(c.state_dir) / "auth-dark"


def test_auth_dark_first_pass_marks_but_does_not_alert(tmp_path, monkeypatch):
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_silent_during_grace_and_since_survives(tmp_path, monkeypatch):
    """Second pass inside the grace window must not alert and must not
    rewrite 'since' — exercises the _now()->fromisoformat round-trip."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)                        # pass 1: creates marker
    since_1 = json.loads(dark_marker(c).read_text())["since"]
    main.run_pass(c, d)                        # pass 2: still within grace
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert st["since"] == since_1              # since not rewritten
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_alerts_once_after_grace(tmp_path, monkeypatch):
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)                       # creates the marker
    old = (datetime.now(timezone.utc)
           - timedelta(minutes=main.AUTH_DARK_GRACE_MINUTES + 1)).isoformat()
    dark_marker(c).write_text(json.dumps({"since": old, "alerted": False}))
    main.run_pass(c, d)                       # past grace -> alert
    main.run_pass(c, d)                       # already alerted -> silent
    assert d.notifier.sent.count("auth_dark") == 1
    assert json.loads(dark_marker(c).read_text())["alerted"] is True


def test_auth_dark_clears_on_recovery(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    patch_usage_dark(monkeypatch)
    main.run_pass(c, d)
    assert dark_marker(c).exists()
    patch_usage(monkeypatch, util=0.2)        # usage readable again
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent


def test_budget_full_with_live_source_never_touches_auth_dark(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)       # full window, oauth source
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent


def test_unavailable_usage_sends_no_budget_stall(tmp_path, monkeypatch):
    """An auth outage fail-safes to no-spawns, but it is not a budget stall:
    "usage window exhausted; stalled until reset" would send the operator to
    wait out an outage only a re-login ends, 30 minutes before the accurate
    auth_dark alert. auth-dark owns the unavailable case."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    main.run_pass(c, d)
    assert "budget_stall" not in d.notifier.sent
    assert not (Path(c.state_dir) / "budget-stalled").exists()


def test_full_window_with_readable_usage_still_sends_budget_stall(
        tmp_path, monkeypatch):
    """Guard on the other side of the suppression: a genuinely exhausted
    window with a live source keeps its stall ping (and its resume)."""
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_stall") == 1
    assert (Path(c.state_dir) / "budget-stalled").exists()
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_resume") == 1


def test_auth_dark_corrupt_marker_is_repaired(tmp_path, monkeypatch):
    """Non-JSON marker must not crash the pass; it is reset to fresh state."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    dark_marker(c).write_text("not valid json{{")
    main.run_pass(c, d)                        # must not raise
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_malformed_marker_is_repaired(tmp_path, monkeypatch):
    """Parseable-but-malformed JSON (null, missing 'since') must not crash the
    pass; the marker is reset to fresh state."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    dark_marker(c).write_text(json.dumps(None))   # valid JSON, non-dict
    main.run_pass(c, d)                            # must not raise
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def test_pass_writes_heartbeat(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    d = deps()
    main.run_pass(c, d)
    hb = json.loads((Path(c.state_dir) / "pass.json").read_text())
    assert hb["interval_minutes"] == c.pass_interval_minutes
    assert hb["started_at"] <= hb["finished_at"]


def test_reply_to_a_running_task_is_queued_not_dropped(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, park="")           # live, unparked
    main.intents.write_intent(c.state_dir, "reply", 42, {"text": "use oauth"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    queued = messages.undelivered(c.state_dir, 42)
    assert [m.text for m in queued] == ["use oauth"]
    assert [m.actor for m in queued] == ["jesdi@github"]
    # a running task is NOT woken — mail waits for the next boundary
    assert load(c.state_dir, 42).park == ""


def test_reply_to_a_wake_pending_task_is_queued_and_not_clobbered(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE)
    for i, text in enumerate(["first", "second"], start=1):
        main.intents.write_intent(c.state_dir, "reply", 42, {"text": text},
                                  "jesdi@github", i)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, 42)] == [
        "first", "second"]


def test_reply_to_an_unclaimed_issue_is_held(tmp_path):
    c = cfg(tmp_path)
    main.intents.write_intent(c.state_dir, "reply", 777,
                              {"text": "pre-brief: use the v2 API"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, 777)] == [
        "pre-brief: use the v2 API"]


def test_reply_to_a_done_task_is_held(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.DONE)
    main.intents.write_intent(c.state_dir, "reply", 42, {"text": "one more"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert len(messages.undelivered(c.state_dir, 42)) == 1


def test_reply_to_a_parked_task_queues_and_requests_a_wake(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN)
    main.intents.write_intent(c.state_dir, "reply", 42, {"text": "use oauth"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, 42)] == [
        "use oauth"]
    assert load(c.state_dir, 42).park == PARK_WAKE


def test_resume_intent_queues_its_text_as_a_message(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_HUMAN)
    main.intents.write_intent(c.state_dir, "resume", 42, {}, "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    texts = [m.text for m in messages.undelivered(c.state_dir, 42)]
    assert texts == ["The operator resumed this task. Continue."]
    assert load(c.state_dir, 42).park == PARK_WAKE


def test_ci_conclusion_becomes_a_dispatcher_message(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_CI, ci_run_id=4242)
    main._wake_ci(c, deps(gh=FakeGitHub(run_conclusion="failure")),
                  c.targets[0])
    from dispatcher import messages
    queued = messages.undelivered(c.state_dir, 42)
    assert len(queued) == 1
    assert "4242 concluded: failure" in queued[0].text
    assert queued[0].actor == "dispatcher"
    t = load(c.state_dir, 42)
    assert t.park == PARK_WAKE and t.ci_run_id == 0


def test_spawn_appends_queued_messages_to_the_stage_prompt(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED)
    from dispatcher import messages
    messages.append(c.state_dir, 42, "pre-brief: use the v2 API", "jesdi@github")
    d = deps()
    task = load(c.state_dir, 42)
    main._spawn_stage(c, d, c.targets[0], task, Stage.SPEC)
    prompt = d.sessions.spawned[-1][3]
    assert "## Operator messages" in prompt
    assert "pre-brief: use the v2 API" in prompt
    assert messages.undelivered(c.state_dir, 42) == []
    assert messages.all_messages(c.state_dir, 42)[0].delivered_at != ""


def test_spawn_without_messages_leaves_the_prompt_untouched(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED)
    d = deps()
    main._spawn_stage(c, d, c.targets[0], load(c.state_dir, 42), Stage.SPEC)
    assert "## Operator messages" not in d.sessions.spawned[-1][3]


def test_resume_delivers_every_queued_message_oldest_first(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE, slot=NO_SLOT)
    from dispatcher import messages
    messages.append(c.state_dir, 42, "first", "jesdi@github")
    messages.append(c.state_dir, 42, "second", "jesdi@github")
    d = deps()
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    text = d.sessions.resumed[-1][1]
    assert text.index("first") < text.index("second")
    assert messages.undelivered(c.state_dir, 42) == []


def test_resume_with_an_empty_queue_still_says_continue(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE, slot=NO_SLOT)
    d = deps()
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    assert d.sessions.resumed[-1][1] == "Continue."


def test_retry_plan_delivers_queued_messages_too(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN)
    from dispatcher import messages
    messages.append(c.state_dir, 42, "keep the scope small", "jesdi@github")
    d = deps()
    main._retry_plan(c, d, c.targets[0], load(c.state_dir, 42),
                     "missing Goal line")
    assert "keep the scope small" in d.sessions.resumed[-1][1]
    assert messages.undelivered(c.state_dir, 42) == []


def test_delivery_does_not_stamp_messages_queued_after_the_drain(tmp_path):
    """A message that lands between the drain and the stamp must survive."""
    c = cfg(tmp_path)
    make_task(c, issue=42, park=PARK_WAKE, slot=NO_SLOT)
    from dispatcher import messages
    messages.append(c.state_dir, 42, "delivered now", "jesdi@github")
    d = deps()
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    messages.append(c.state_dir, 42, "arrived later", "jesdi@github")
    assert [m.text for m in messages.undelivered(c.state_dir, 42)] == [
        "arrived later"]


def test_park_for_input_frees_the_slot(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=1)
    main._park_for_input(c, deps(), c.targets[0], load(c.state_dir, 42),
                         note="which redirect URL?")
    t = load(c.state_dir, 42)
    assert t.park == PARK_HUMAN and t.slot == NO_SLOT


def test_park_for_ci_frees_the_slot(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=2)
    main._park_for_ci(c, deps(), c.targets[0], load(c.state_dir, 42),
                      run_id=4242)
    t = load(c.state_dir, 42)
    assert t.park == PARK_CI and t.slot == NO_SLOT


def test_login_park_keeps_its_slot(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=1, park=PARK_LOGIN)
    main._reconcile_slots(c)
    assert load(c.state_dir, 42).slot == 1


def test_reconcile_frees_slots_leaked_by_older_code(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=0, park=PARK_HUMAN)
    make_task(c, issue=43, slot=1, park=PARK_WAKE)
    make_task(c, issue=44, slot=2, park="")          # live: untouched
    main._reconcile_slots(c)
    assert load(c.state_dir, 42).slot == NO_SLOT
    assert load(c.state_dir, 43).slot == NO_SLOT
    assert load(c.state_dir, 44).slot == 2


def test_reconcile_emits_one_event_per_freed_slot(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=0, park=PARK_HUMAN)
    main._reconcile_slots(c)
    main._reconcile_slots(c)                          # already NO_SLOT: no-op
    freed = [e for e in main.eventlog.read_tail(c.state_dir)
             if e["event"] == "slot-reclaimed"]
    assert len(freed) == 1 and freed[0]["issue"] == 42


def test_two_human_parks_no_longer_deadlock_a_resume(tmp_path):
    """The live 2026-08-12 failure: two PARK_HUMAN tasks plus one running
    session held every slot while capacity showed headroom, and the woken
    task was skipped every pass."""
    c = dc_replace(cfg(tmp_path), capacity=2)
    make_task(c, issue=197, slot=0, park=PARK_HUMAN)
    make_task(c, issue=267, slot=1, park=PARK_HUMAN)
    make_task(c, issue=240, slot=2, park="")
    make_task(c, issue=198, slot=NO_SLOT, park=PARK_WAKE)
    main._reconcile_slots(c)
    d = deps()
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    t = load(c.state_dir, 198)
    assert t.park == "" and t.slot != NO_SLOT


def test_blocked_wake_emits_one_event_not_one_per_pass(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, park="")          # holds the only capacity
    make_task(c, issue=42, slot=NO_SLOT, park=PARK_WAKE)
    d = deps()
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    blocked = [e for e in main.eventlog.read_tail(c.state_dir)
               if e["event"] == "wake-blocked"]
    assert len(blocked) == 1
    assert blocked[0]["issue"] == 42
    assert blocked[0]["detail"] == "capacity full"
    assert main._wake_blocked_path(c, 42).exists()


def test_slot_exhaustion_is_reported_as_such(tmp_path, monkeypatch):
    """Practically unreachable now that max_slots == capacity + 2 (the
    capacity gate fires first), but the guard stays — and when it does fire it
    must name the resource that ran out, not just go quiet."""
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=NO_SLOT, park=PARK_WAKE)
    monkeypatch.setattr(main, "allocate_slot", lambda *a, **kw: None)
    main._resume_woken(c, deps(), c.targets[0], budget_ok=True)
    blocked = [e for e in main.eventlog.read_tail(c.state_dir)
               if e["event"] == "wake-blocked"]
    assert [e["detail"] for e in blocked] == ["no free slot"]


def test_marker_clears_once_the_wake_succeeds(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, park="")
    make_task(c, issue=42, slot=NO_SLOT, park=PARK_WAKE)
    d = deps()
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    assert main._wake_blocked_path(c, 42).exists()
    main.delete(c.state_dir, 41)                     # capacity frees up
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    assert not main._wake_blocked_path(c, 42).exists()
    assert load(c.state_dir, 42).park == ""


def test_blocked_feedback_spawn_is_reported_too(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, park="")
    make_task(c, issue=42, slot=NO_SLOT, stage=Stage.PR_OPEN,
              feedback_pending=True)
    main._spawn_feedback(c, deps(), c.targets[0], budget_ok=True)
    blocked = [e for e in main.eventlog.read_tail(c.state_dir)
               if e["event"] == "wake-blocked"]
    assert [e["issue"] for e in blocked] == [42]


def test_kill_stops_the_task_waiting_for_a_slot_and_drops_its_marker(
        tmp_path, monkeypatch):
    """A terminal transition must not leave the wake-blocked marker behind:
    the killed card would render "waiting for a free slot" forever, and a
    later re-claim of the same issue would inherit the stale badge and the
    stale delivery_contract suffix. The marker lifecycle is healed from disk
    by the reconcile sweep, not by every terminal path remembering to unlink."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, park="")      # holds the only capacity unit
    make_task(c, issue=42, slot=NO_SLOT, park=PARK_WAKE)
    d = deps(sess=FakeSessions(alive={41}))
    main._resume_woken(c, d, c.targets[0], budget_ok=True)
    assert main._wake_blocked_path(c, 42).exists()

    intents_mod.write_intent(c.state_dir, "kill", 42, {}, "op", 1)
    main.run_pass(c, d)
    t = load(c.state_dir, 42)
    assert t.stage is Stage.FAILED
    assert t.park == ""                          # no longer waiting for a wake
    main.run_pass(c, d)                          # the sweep heals it from disk
    assert not main._wake_blocked_path(c, 42).exists()


def test_reconcile_keeps_the_marker_of_a_task_that_is_still_starving(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, park="")
    make_task(c, issue=42, slot=NO_SLOT, park=PARK_WAKE)
    make_task(c, issue=43, slot=NO_SLOT, stage=Stage.PR_OPEN,
              feedback_pending=True)
    main._wake_blocked_path(c, 42).touch()
    main._wake_blocked_path(c, 43).touch()
    main._reconcile_slots(c)
    assert main._wake_blocked_path(c, 42).exists()   # wake still denied
    assert main._wake_blocked_path(c, 43).exists()   # feedback spawn still denied


def test_reconcile_drops_a_marker_left_by_a_vanished_task(tmp_path):
    c = cfg(tmp_path)
    main._wake_blocked_path(c, 42).parent.mkdir(parents=True, exist_ok=True)
    main._wake_blocked_path(c, 42).touch()      # state file already flushed
    main._reconcile_slots(c)
    assert not main._wake_blocked_path(c, 42).exists()


def test_console_reply_to_a_login_park_types_the_code(tmp_path, monkeypatch):
    """PARK_LOGIN is the one park whose "reply" is an OAuth authorization
    code. Queueing it would promise a delivery that cannot work (the resume
    ends the pane the code was for) and would persist a single-use credential
    at rest, rendered in the console thread."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.SPEC, park=PARK_LOGIN, park_msg_id=77)
    intents_mod.write_intent(c.state_dir, "reply", 42,
                             {"text": "oauth-code-abc#123"}, "jesdi@github", 1)
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    assert sess.sent_text == [(42, "oauth-code-abc#123")]
    assert sess.resumed == []                   # never through the wake path
    assert messages.all_messages(c.state_dir, 42) == []  # not persisted at rest
    assert load(c.state_dir, 42).park == ""


def test_console_reply_to_a_human_park_still_queues(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, park=PARK_HUMAN, park_msg_id=55)
    make_task(c, issue=43)                      # holds the only capacity unit
    intents_mod.write_intent(c.state_dir, "reply", 42, {"text": "use oauth"},
                             "jesdi@github", 1)
    sess = FakeSessions(alive={43})
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    assert sess.sent_text == []
    assert [m.text for m in messages.all_messages(c.state_dir, 42)] == [
        "use oauth"]
