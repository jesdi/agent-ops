import json
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import (Stage, TaskState, load, save, SessionRecord, write_session)
from tests.configfakes import with_project
from tests.dispatchfakes import (FakeGitHub, FakeSessions,
                                 cfg, deps, make_task, patch_usage,
                                 patch_workspace, replace_capacity)
from tests.statuses import human, waking, failed
from dispatcher.status import Kind


def test_digest(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.PLAN, gated=True, slot=0,
                                worktree="/x", branch="b", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    d = deps()
    main.send_digest(c, d)
    assert d.notifier.sent == ["daily_digest"]


def test_woken_task_resumes_before_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    c = replace_capacity(c, 1)
    wt = make_task(c, status=waking())
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert [(issue, text.split("\n\n")[0], model, effort)
            for issue, text, model, effort in sess.resumed] == [(42, "Continue.", "anthropic/claude-opus-5", "")]
    assert gh.claimed == []  # head-of-queue: resume consumed the only slot
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and t.park_msg_id == 0
    sig = json.loads((wt / ".agent" / "stage.json").read_text())
    assert sig["status"] == "working"  # rewritten before resume


def test_parked_task_does_not_block_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, status=human(), park_msg_id=55)
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [99]


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
    the launch raised inside sessions.resume, guarded_pass re-raised, and
    _drive_wakes never reached the tasks behind it — no claims, no spawns,
    nothing, every pass, until a human looked."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    write_session(c.state_dir, "portfolio_eval", 43, SessionRecord("other-session", "implement"))
    make_task(c, issue=42, status=waking(), slot=0,
              updated_at="2026-07-21T00:00:00+00:00")
    make_task(c, issue=43, status=waking(), slot=1,
              updated_at="2026-07-22T00:00:00+00:00")
    gh = FakeGitHub()
    sess = FakeSessions(resume_raises=[42])
    d = deps(gh, sess)

    main.run_pass(c, d)  # must not raise

    # The task behind the broken one still got its turn.
    assert [r[0] for r in sess.resumed] == [43]
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.RUNNING
    # The broken one is failed by the crash and off the board's live columns.
    broken = load(c.state_dir, "portfolio_eval", 42)
    assert broken.status == failed("task-crash")
    assert (42, "task crashed mid-pass") in gh.released
    # Filed on the infra repo: a vanished worktree is a box-side problem
    # no session container can fix.
    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops"
    assert "task-crash" in title and "#42" in title
    assert "No such file or directory" in body
    assert "task_failed" in d.notifier.sent


def test_broken_worktree_on_spawn_fails_that_task_and_pass_survives(
        tmp_path, monkeypatch):
    """Same isolation on the spawn path: _drive_task advancing a stage into
    a vanished worktree fails the task, not the pass."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    # Gate stage with a dead session: _drive_task respawns it in place.
    make_task(c, issue=42, stage=Stage.PLAN, gated=True, slot=0)
    gh = FakeGitHub()
    sess = FakeSessions(spawn_raises=[42])
    d = deps(gh, sess)

    main.run_pass(c, d)  # must not raise

    assert sess.spawned == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert "task-crash" in gh.created_issues[0][1]


def test_repository_specific_policy_is_used_at_spawn(tmp_path, monkeypatch):
    """Finding D: a per-repository `models:` override was only ever verified in
    isolation against `policy_for` — never end-to-end through `_spawn_stage`.
    The global POLICY's untracked/standard track resolves to Opus; the
    repository's OWN policy must win instead and resolve to its own model."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    repository_policy = parse_policy({
        "triage": ["claude-sonnet-5"],
        "untracked": "standard",
        "tracks": {"standard": {
            "when": "Everything.",
            "spec": ["claude-fable-5-1"], "plan": ["claude-fable-5-1"],
            "implement": ["claude-fable-5-1"], "review": ["claude-fable-5-1"]}},
    })
    c = dc_replace(c, repositories=[with_project(c.repositories[0], models=repository_policy)])
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=1, labels=("auto",))])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "anthropic/claude-fable-5-1")]


def test_migrate_tmux_flag_migrates_and_its_callback_really_wakes(
        tmp_path, monkeypatch):
    """provision/update.sh runs this under `set -e`, so a wiring slip here
    fails the whole convergence pass on every firing while any tmux session
    exists. Assert the flag reaches tmux_migration.migrate with the state
    dir and a wake callable that IS `_wake` — park=PARK_WAKE plus a queued
    message — and that the branch takes no pass_lock (update.sh already
    holds that file; flock is per open-file-description, so re-taking it
    would block forever)."""
    import sys as _sys
    from dispatcher import messages

    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT)
    monkeypatch.setattr(main, "load_config", lambda path: c)

    def _no_lock(state_dir):
        raise AssertionError("--migrate-tmux must not take pass_lock")

    monkeypatch.setattr(main, "pass_lock", _no_lock)

    seen = {}

    def fake_migrate(state_dir, wake):
        seen["state_dir"], seen["wake"] = state_dir, wake
        return ["killed tmux session task-portfolio_eval-42"]

    monkeypatch.setattr(main.tmux_migration, "migrate", fake_migrate)
    monkeypatch.setattr(_sys, "argv",
                        ["agent-ops-dispatcher", "--config",
                         str(tmp_path / "targets.yaml"), "--migrate-tmux"])

    main.main()

    assert seen["state_dir"] == c.state_dir
    assert callable(seen["wake"])
    # The callback is the real _wake: driving it must park the task for a
    # resume and queue the text the resumed claude will be handed.
    seen["wake"](load(c.state_dir, "portfolio_eval", 42), "moved to herdr")
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING
    assert [m.text for m in messages.all_messages(c.state_dir, "portfolio_eval", 42)] == [
        "moved to herdr"]
