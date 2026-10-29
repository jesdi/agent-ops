"""A merged task PR closes the task's issue (spec: let-task-pr-base, req. 7).

Seam: main.run_pass with a fake GitHub that reports the PR as merged and
records issue state and close calls. The fake's `close_issue` is the
interface the box must call to close an issue (any positional/keyword shape).
"""
import subprocess
from dataclasses import replace as dc_replace

import dispatcher.main as main
from dispatcher.state import Stage, load
from tests.configfakes import with_board
from tests.test_main import (FakeGitHub, deps, cfg, patch_teardown,
                             patch_usage, payload, pr_open_task)

REPO = "jesdi/portfolio_eval"
MERGED = dict(state="MERGED", merged_at="2026-07-30T10:00:00Z")


class ClosingGitHub(FakeGitHub):
    def __init__(self, *a, close_raises=False, **kw):
        super().__init__(*a, **kw)
        self.close_raises = close_raises
        self.close_calls = []

    def close_issue(self, *args, **kw):
        self.close_calls.append(args + tuple(kw.values()))
        if self.close_raises:
            raise subprocess.CalledProcessError(1, ["gh"])
        given = args + tuple(kw.values())
        number = next(a for a in given if isinstance(a, int))
        repo = next(a for a in given if isinstance(a, str))
        self.issue_states[(repo, number)] = "CLOSED"


def _setup(tmp_path, monkeypatch, issue, pr, states):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    base = cfg(tmp_path)
    c = dc_replace(base, repositories=[with_board(
        base.repositories[0], status_done_option_id="D0")])
    t = pr_open_task(c, issue=issue, pr_number=pr)
    gh = ClosingGitHub(issue_states=states)
    gh.pr_payloads[pr] = payload(**MERGED)
    return c, t, gh, removed


def _stage(c, issue):
    return load(c.state_dir, "portfolio_eval", issue).stage


def test_merged_pr_closes_the_open_issue_and_finishes_the_task(
        tmp_path, monkeypatch):
    c, t, gh, removed = _setup(tmp_path, monkeypatch, 42, 57,
                               {(REPO, 42): "OPEN"})
    main.run_pass(c, deps(gh))
    assert gh.issue_states[(REPO, 42)] == "CLOSED"
    assert gh.close_calls == [(REPO, 42)]
    assert _stage(c, 42) is Stage.DONE


def test_merged_pr_leaves_an_already_closed_issue_alone(tmp_path, monkeypatch):
    c, t, gh, removed = _setup(tmp_path, monkeypatch, 43, 58,
                               {(REPO, 43): "CLOSED"})
    main.run_pass(c, deps(gh))
    assert gh.close_calls == []
    assert [i for i, _ in gh.comments] == []
    assert _stage(c, 43) is Stage.DONE


def test_failed_close_keeps_the_task_and_worktree_until_the_next_pass(
        tmp_path, monkeypatch):
    c, t, gh, removed = _setup(tmp_path, monkeypatch, 42, 57,
                               {(REPO, 42): "OPEN"})
    gh.close_raises = True
    main.run_pass(c, deps(gh))
    assert _stage(c, 42) is Stage.PR_OPEN
    assert removed == []
    assert gh.deleted_branches == []
    gh.close_raises = False
    main.run_pass(c, deps(gh))
    assert gh.issue_states[(REPO, 42)] == "CLOSED"
    assert _stage(c, 42) is Stage.DONE
    assert removed == [(t.worktree, t.branch, False)]


def test_dry_run_makes_no_close_call(tmp_path, monkeypatch):
    c, t, gh, removed = _setup(tmp_path, monkeypatch, 42, 57,
                               {(REPO, 42): "OPEN"})
    main.run_pass(c, deps(gh), dry_run=True)
    assert gh.close_calls == []
