"""Acceptance tests for ticket 04 (project-onboarding): Telegram replies and
commands reach the repository-qualified task they name.

Spec: specs/project-onboarding/spec.md requirement 24, scenario S57.
Seam: TASK_API's Telegram surface, driven through dispatcher.main.run_pass
with the inbound events patched, as tests/test_main.py does.

Interface these tests define: `telegram.inbound.Command` gains `repo: str = ""`
and `classify("/attach owner/name#12", 0)` fills it; a bare `/attach 12` stays
valid and means "the one parked task numbered 12".
"""
from pathlib import Path

import dispatcher.main as main
from dispatcher import messages
from dispatcher.state import NO_SLOT, PARK_HUMAN, PARK_WAKE, Stage, TaskState, load, save
from telegram.inbound import Command, Reply, classify
from tests.test_main import (FakeNotifier, FakeSessions, cfg, deps, make_task,
                             patch_events, patch_usage, patch_workspace,
                             replace_capacity)

PROJECT = "portfolio_eval"
FRONT, BACK = "example/shop-frontend", "example/shop-backend"
ISSUE = 12


def parked(c, repo, msg_id):
    wt = Path(c.targets[0].worktrees_path) / f"task-{repo.replace('/', '-')}"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    save(c.state_dir, TaskState(
        issue=ISSUE, target=PROJECT, stage=Stage.IMPLEMENT, slot=NO_SLOT,
        worktree=str(wt), branch=f"agent/{repo.replace('/', '-')}", title=repo,
        updated_at="2026-10-16T00:00:00+00:00", track="standard",
        park=PARK_HUMAN, park_msg_id=msg_id, repo=repo))


def run(monkeypatch, tmp_path, events, repos):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=43)  # the active task keeps the only slot: a woken task stays woken
    for repo, msg_id in repos:
        parked(c, repo, msg_id)
    patch_events(monkeypatch, events)
    d = deps(sess=FakeSessions(alive={43}), notifier=FakeNotifier())
    main.run_pass(c, d)
    return c, d


def test_reply_reaches_exactly_the_task_that_sent_the_notification(tmp_path, monkeypatch):
    c, _ = run(monkeypatch, tmp_path, [Reply(reply_to_msg_id=56, text="use oauth")],
               [(FRONT, 55), (BACK, 56)])

    assert load(c.state_dir, PROJECT, ISSUE, repo=BACK).park == PARK_WAKE
    assert load(c.state_dir, PROJECT, ISSUE, repo=FRONT).park == PARK_HUMAN
    assert [m.text for m in messages.undelivered(c.state_dir, PROJECT, ISSUE, repo=BACK)] == ["use oauth"]
    assert messages.undelivered(c.state_dir, PROJECT, ISSUE, repo=FRONT) == []


def test_qualified_attach_names_its_repository():
    ev = classify(f"/attach {BACK}#{ISSUE}", 0)
    assert ev is not None and ev.issue == ISSUE
    assert getattr(ev, "repo", "") == BACK


def test_bare_attach_is_ambiguous_across_repositories(tmp_path, monkeypatch):
    c, d = run(monkeypatch, tmp_path, [Command(name="attach", issue=ISSUE)],
               [(FRONT, 55), (BACK, 56)])
    assert load(c.state_dir, PROJECT, ISSUE, repo=FRONT).park == PARK_HUMAN
    assert load(c.state_dir, PROJECT, ISSUE, repo=BACK).park == PARK_HUMAN
    said = "\n".join(line for _, ctx in d.notifier.calls for line in ctx.get("lines", []))
    assert FRONT in said and BACK in said

    # One parked task numbered 12, whose repository is not proven: the bare
    # reference still reaches it.
    c, _ = run(monkeypatch, tmp_path / "legacy", [Command(name="attach", issue=ISSUE)],
               [("", 57)])
    assert load(c.state_dir, PROJECT, ISSUE).park == PARK_WAKE


def test_qualified_attach_wakes_only_the_named_repository(tmp_path, monkeypatch):
    ev = classify(f"/attach {BACK}#{ISSUE}", 0)
    assert ev is not None
    c, _ = run(monkeypatch, tmp_path, [ev], [(FRONT, 55), (BACK, 56)])
    assert load(c.state_dir, PROJECT, ISSUE, repo=BACK).park == PARK_WAKE
    assert load(c.state_dir, PROJECT, ISSUE, repo=FRONT).park == PARK_HUMAN
