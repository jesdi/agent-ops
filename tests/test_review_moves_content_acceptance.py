"""Acceptance tests for ticket 07: the review stage leaves only spec.md behind.

Spec: specs/2026-10-06-openspec-pipeline/spec.md, requirements 17-20 and their
scenarios; design.md (the review session writes the pull request description
to a file before it removes anything; the change-log rule lives in the one
prompt part every stage already gets). Black-box through
dispatcher.main.run_pass over the fakes of tests/test_main.py. Every prompt
assertion is scoped to the paragraph (or ordered paragraphs) that carries the
rule, never to the whole prompt.
"""
import json
import re
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import PlanApprovalRequest, Stage, load, save

from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps, make_task,
                             patch_usage, patch_workspace, pr_open_task,
                             valid_spec, write_tickets, payload)
from tests.usagefakes import session_usage

ISSUE = 412
BRANCH = "agent/412-csv-export"
SPEC = "specs/2026-10-12-csv-export/spec.md"
STAGES = ["spec", "plan", "implement", "review", "address-review"]


def _setup(tmp_path, monkeypatch, review="claude-sonnet-5"):
    patch_usage(monkeypatch, util=0.2)
    patch_workspace(monkeypatch, tmp_path)
    m = ["claude-sonnet-5"]
    policy = parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {"standard": {"when": "The standard track.", "spec": m,
                                "plan": m, "implement": m, "review": [review]}}})
    return dc_replace(cfg(tmp_path), models=policy)


def _signal(wt, **kw):
    (wt / ".agent" / "stage.json").write_text(json.dumps(kw))


def _task(c):
    return load(c.state_dir, "portfolio_eval", ISSUE)


def _spawns(sess, stage):
    return [s for s in sess.spawned if s[1] == stage]


def _implement_started(c):
    wt = make_task(c, issue=ISSUE, stage=Stage("awaiting-plan-review"),
                   spec_path=SPEC,
                   operator_request=PlanApprovalRequest(".agent/plan-review.md"))
    save(c.state_dir, dc_replace(_task(c), branch=BRANCH,
                                 title="Export a portfolio as CSV"))
    write_tickets(wt, 4)
    _signal(wt, stage="plan", status="done", note="approved",
            artifact=".agent/tickets", track="standard")
    sess = FakeSessions(alive={ISSUE})
    main.run_pass(c, deps(sess=sess))
    assert _task(c).stage is Stage.IMPLEMENT
    return wt, sess


def _review_spawn(c):
    wt, sess = _implement_started(c)
    _signal(wt, stage="implement", status="done", note="4/4 tickets merged")
    main.run_pass(c, deps(sess=sess))
    (spawn,) = _spawns(sess, "review")
    return spawn


def _prompt(tmp_path, monkeypatch, stage):
    c = _setup(tmp_path, monkeypatch)
    if stage == "spec":
        gh = FakeGitHub([Candidate(ISSUE, "Export a portfolio as CSV", "u")])
        sess = FakeSessions()
        main.run_pass(c, deps(gh, sess))
    elif stage == "plan":
        wt = make_task(c, issue=ISSUE, stage=Stage.SPEC, track="standard")
        valid_spec(wt)
        _signal(wt, stage="spec", status="done", artifact="spec.md",
                track="standard")
        sess = FakeSessions(alive={ISSUE})
        main.run_pass(c, deps(sess=sess))
    elif stage == "implement":
        _, sess = _implement_started(c)
    elif stage == "review":
        return _review_spawn(c)[3]
    else:
        pr_open_task(c, issue=ISSUE, spec_path=SPEC, feedback_pending=True)
        gh = FakeGitHub()
        gh.pr_payloads[12] = payload()
        sess = FakeSessions()
        main.run_pass(c, deps(gh, sess))
    (spawn,) = _spawns(sess, stage)
    return spawn[3]


def _paras(prompt):
    return [p.replace("\n", " ").lower() for p in re.split(r"\n\s*\n", prompt)]


def _rule(prompt, *words):
    hits = [p for p in _paras(prompt) if all(w in p for w in words)]
    assert hits, f"no paragraph holds {words}"
    return hits


def _first(prompt, pred, what):
    for i, p in enumerate(_paras(prompt)):
        if pred(p):
            return i
    pytest.fail(f"no paragraph: {what}")


# 1. Review runs on the other provider and opens the pull request.
def test_review_starts_on_the_openai_entry_and_closes_the_issue(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch, review="openai/gpt-5-codex")
    monkeypatch.setattr(main, "fetch_all", lambda cfg, **k: {
        "anthropic": session_usage(0.2),
        "openai": session_usage(0.2, provider="openai")})
    spawn = _review_spawn(c)
    assert spawn[2] == "openai/gpt-5-codex"
    assert _task(c).picks["implement"].startswith("anthropic/")
    (para, *_) = _rule(spawn[3], "gh pr create")
    assert "Closes #412" in " ".join(
        p for p in re.split(r"\n\s*\n", spawn[3]) if "gh pr create" in p)


# 2. Own review first, ledger after.
def test_review_prompt_reads_the_ledger_only_after_its_own_review(tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch, "review")
    for para in _rule(prompt, ".agent/ledger.md"):
        if "own review" in para and re.search(
                r"only after|not before|never before|not (read|open)[^.]*before", para):
            return
    pytest.fail("no paragraph holds the ledger with the own-review-first rule")


# 3. The pull request description is written to a file first.
def test_review_prompt_writes_the_description_file_from_both_documents(
        tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch, "review")
    for para in _rule(prompt, ".agent/pr-body.md", "proposal.md", "design.md",
                      "ledger"):
        if (all(re.search(r, para) for r in (
                r"\bwhy\b", r"(?<!non-)\bgoal\b", r"\bnon-goals\b",
                r"\bdecisions\b", r"open rulings?"))
                and re.search(r"already exist|if it exists|if the file exists", para)
                and "write" in para):
            return
    pytest.fail("no paragraph writes pr-body.md from proposal, design and ledger "
                "and reuses an existing file")


# 4. ADRs and CONTEXT.md.
def test_review_prompt_adds_adrs_and_new_terms(tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch, "review")
    (adr, *_) = _rule(prompt, "docs/adr/", "constrain")
    assert "adr" in adr and re.search(r"add|write|create", adr)
    assert "each decision" in adr or "every decision" in adr
    (term, *_) = _rule(prompt, "context.md", "new term")
    assert re.search(r"add|write|record", term)


# 5. Order: description file, removal, pull request.
def test_review_prompt_removes_the_documents_between_description_and_pull_request(
        tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch, "review")
    write = _first(prompt, lambda p: ".agent/pr-body.md" in p and "write" in p,
                   "writes .agent/pr-body.md")
    remove = _first(prompt, lambda p: all(w in p for w in (
        "proposal.md", "design.md", "docs/review/"))
        and re.search(r"remove|delete|git rm", p), "removes proposal, design, docs/review")
    create = _first(prompt, lambda p: "gh pr create" in p, "opens the pull request")
    assert write < remove < create
    para = _paras(prompt)[remove]
    assert "this task" in para and "added" in para


# 6. Every stage says specs are history.
@pytest.mark.parametrize("stage", STAGES)
def test_stage_prompt_says_specs_are_a_change_log_and_code_wins(
        tmp_path, monkeypatch, stage):
    prompt = _prompt(tmp_path, monkeypatch, stage)
    for para in _rule(prompt, "specs/", "change log", "context.md"):
        if "present state" in para and re.search(
                r"code wins|code[^.]*(wins|prevails|takes precedence)", para):
            return
    pytest.fail(f"{stage}: no paragraph holds change log, present state and code wins")


# 7. No stage commits review copies.
@pytest.mark.parametrize("stage", STAGES)
def test_no_stage_prompt_commits_a_copy_under_docs_review(tmp_path, monkeypatch, stage):
    prompt = _prompt(tmp_path, monkeypatch, stage)
    verbs = r"commit|copy|copies|write|save|push|publish|put|keep"
    for para in _paras(prompt):
        if "docs/review/" not in para or not re.search(verbs, para):
            continue
        removal = (stage == "review" and re.search(r"remove|delete|git rm", para)
                   and "proposal.md" in para
                   and not re.search(r"commit|cop(y|ies)", para))
        assert removal, f"{stage}: tells a session to put content under docs/review/: {para}"


# 8. Address-review names the task's spec.
def test_address_review_prompt_names_the_spec_and_not_the_removed_files(
        tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch, "address-review")
    _rule(prompt, "2026-10-12-csv-export/spec.md")
    for gone in ("proposal.md", "design.md"):
        assert gone not in prompt.lower()
