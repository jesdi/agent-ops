"""Ticket 03: every stage prompt and the PR work against the task's base.

Black-box through dispatcher.main.run_pass: a saved task (or a conflicting PR,
or a fresh claim) makes the box launch a stage; the test reads the prompt the
fake session launcher receives.
"""
import json
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher.github import Candidate
from dispatcher.state import Stage

from tests.test_claim_base_acceptance import BodyGitHub
from tests.test_main import (FakeSessions, cfg, deps, make_task, patch_usage,
                             patch_workspace, payload, pr_open_task, write_tickets)

NEW = "feat/new-cart"
SPEC_TEXT = "# t — design\n\n## Problem\n\n" + "x " * 400 + "\n\n## Decisions\n\n" + "y " * 400


@pytest.fixture
def c(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return cfg(tmp_path)


def signal(wt, **fields):
    (wt / ".agent" / "stage.json").write_text(json.dumps(fields))


def run_for(c, stage, gh=None, **task_fields):
    """Run one pass that launches `stage`; return the prompt it was given."""
    gh = gh or BodyGitHub({})
    if stage == "spec":
        gh = BodyGitHub({42: f"Base: {NEW}\n"}, cands=[Candidate(42, "t", "u42")])
    elif stage == "plan":
        wt = make_task(c, stage=Stage.SPEC, **task_fields)
        (wt / "spec.md").write_text(SPEC_TEXT)
        signal(wt, stage="spec", status="done", note="", artifact="spec.md",
               track="standard")
    elif stage == "implement":
        wt = make_task(c, stage=Stage.AWAITING_PLAN_REVIEW, **task_fields)
        write_tickets(wt, 1)
        signal(wt, stage="plan", status="done", note="approved",
               artifact=".agent/tickets")
    elif stage == "review":
        wt = make_task(c, stage=Stage.IMPLEMENT, ticket_count=1, **task_fields)
        signal(wt, stage="implement", status="done", note="")
    elif stage == "address-review":
        pr_open_task(c, **task_fields)
        gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="abc")
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(gh, sess))
    (p,) = [s[3] for s in sess.spawned if s[1] == stage]
    return p


def test_review_diff_and_docs_check_use_the_base(c):
    p = run_for(c, "review", base=NEW)
    assert p.count(f"origin/{NEW}...HEAD") >= 2
    assert "origin/main" not in p


def test_gate_rebases_onto_the_base(c):
    assert f"git rebase origin/{NEW}\n" in run_for(c, "review", base=NEW)


def test_pr_opens_into_the_base_with_closes_first_line(c):
    p = run_for(c, "review", base=NEW)
    assert f"--base {NEW}" in p
    assert "the first line of that body is `Closes #42`" in p


def test_address_review_conflict_rebases_onto_the_base(c):
    p = run_for(c, "address-review", base=NEW)
    assert f"git rebase origin/{NEW}" in p
    assert "origin/main" not in p


def test_base_edited_after_claim_keeps_the_claimed_base(c):
    gh = BodyGitHub({42: "Base: main\n"})
    p = run_for(c, "review", gh, base=NEW)
    assert f"origin/{NEW}...HEAD" in p and f"--base {NEW}" in p
    assert "origin/main" not in p


def test_task_saved_without_a_base_uses_main(c):
    make_task(c, issue=17, stage=Stage.IMPLEMENT, ticket_count=1)
    f = Path(c.state_dir) / "task-portfolio_eval-17.json"
    d = json.loads(f.read_text())
    d.pop("base", None)
    f.write_text(json.dumps(d))
    signal(Path(c.repositories[0].worktrees_path) / "task-17", stage="implement",
           status="done", note="")
    sess = FakeSessions(alive={17})
    main.run_pass(c, deps(sess=sess))
    (p,) = [s[3] for s in sess.spawned if s[1] == "review"]
    assert "origin/main...HEAD" in p
    assert "git rebase origin/main" in p
    assert "--base main" in p


@pytest.mark.parametrize("verify_cmd", ["make e2e-slot SLOT={slot}", ""])
@pytest.mark.parametrize("stage", ["spec", "plan", "implement", "review", "address-review"])
def test_every_stage_renders_for_a_task_with_a_base(c, stage, verify_cmd):
    c = dc_replace(c, repositories=[dc_replace(c.repositories[0], verify_cmd=verify_cmd)])
    assert run_for(c, stage, base=NEW)
