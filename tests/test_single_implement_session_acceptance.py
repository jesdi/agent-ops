"""Acceptance tests for ticket 05: one implement session works every ticket.

Spec: specs/2026-10-06-openspec-pipeline/spec.md, requirements 11-16 and their
scenarios; design.md (Implement is one stage start, a retired task field is
dropped by the loader, a dead implement session fails the task, the implement
prompt points at the skill file). Black-box through dispatcher.main.run_pass
over the fakes of tests/test_main.py. Prompt tests scope every assertion to
the paragraph that carries the rule, never to the whole prompt.
"""
import json
import re
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import pytest

import dispatcher.main as main
from dispatcher import intents as intents_mod
from dispatcher.models import parse_policy
from dispatcher.state import (PARK_HUMAN, PlanApprovalRequest, Stage, TaskState,
                              load, load_all, save)

from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps, make_task,
                             patch_usage, patch_workspace, write_tickets)

ISSUE = 412
BRANCH = "agent/412-csv-export"
SPEC = "specs/2026-10-12-csv-export/spec.md"
SUMMARY = ".agent/plan-review.md"
GATE = "awaiting-plan-review"
TICKETS = 4


def _setup(tmp_path, monkeypatch, util=0.2, **kw):
    patch_usage(monkeypatch, util=util)
    patch_workspace(monkeypatch, tmp_path)
    m = ["claude-sonnet-5"]
    policy = parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {"standard": {"when": "The standard track.", "spec": m,
                                "plan": m, "implement": m, "review": m}}})
    return dc_replace(cfg(tmp_path), models=policy, **kw)


def _signal(wt, **kw):
    (wt / ".agent" / "stage.json").write_text(json.dumps(kw))


def _task(c, issue=ISSUE):
    return load(c.state_dir, "portfolio_eval", issue)


def _approved(c, issue=ISSUE):
    """Issue 412 at the plan gate; the plan session reports done."""
    wt = make_task(c, issue=issue, stage=Stage(GATE), spec_path=SPEC,
                   operator_request=PlanApprovalRequest(SUMMARY))
    save(c.state_dir, dc_replace(_task(c, issue), branch=BRANCH,
                                 title="Export a portfolio as CSV"))
    write_tickets(wt, TICKETS)
    _signal(wt, stage="plan", status="done", note="approved",
            artifact=".agent/tickets", track="standard")
    return wt


def _started(c, tmp_path_unused=None):
    """Task 412 with its implement session started; (worktree, sessions)."""
    wt = _approved(c)
    sess = FakeSessions(alive={ISSUE})
    main.run_pass(c, deps(sess=sess))
    assert _task(c).stage is Stage.IMPLEMENT
    sess.ended.clear()   # the plan session's end is not under test
    return wt, sess


def _implements(sess):
    return [s for s in sess.spawned if s[1] == "implement"]


def _prompt(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _, sess = _started(c)
    (spawn,) = _implements(sess)
    return spawn[3]


def _paras(prompt):
    return [p.replace("\n", " ").lower() for p in re.split(r"\n\s*\n", prompt)]


def _rule(prompt, *words):
    """The paragraphs that hold every one of `words`; at least one."""
    hits = [p for p in _paras(prompt) if all(w in p for w in words)]
    assert hits, f"no paragraph holds {words}"
    return hits


# 1. One session works every ticket.
def test_one_implement_session_for_four_tickets_then_review(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _started(c)
    for n in (1, 2, 3):
        _signal(wt, stage="implement", status="working",
                note=f"{n}/{TICKETS} tickets merged")
        main.run_pass(c, deps(sess=sess))
        assert _task(c).stage is Stage.IMPLEMENT
    assert [s for s in sess.spawned if s[1] == "review"] == []

    _signal(wt, stage="implement", status="done", note="4/4 tickets merged")
    main.run_pass(c, deps(sess=sess))

    assert len(_implements(sess)) == 1
    started = [s[3] for s in sess.spawned] + [r[1] for r in sess.resumed]
    assert not any(re.search(r"ticket\s+\d+\s+of\s+\d+", p.lower()) for p in started)
    assert len([s for s in sess.spawned if s[1] == "review"]) == 1
    assert _task(c).stage is Stage.REVIEW


# 2. Implement is admitted once.
def test_running_implement_session_is_not_stopped_when_the_entry_is_denied_later(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _started(c)           # admitted at util 0.2
    assert len(_implements(sess)) == 1
    _signal(wt, stage="implement", status="working", note="2/4 tickets merged")
    save(c.state_dir, dc_replace(_task(c), updated_at=(
        datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()))
    patch_usage(monkeypatch, util=0.99)   # the same entry would be denied now

    # A second task that wants to start its implement session is denied.
    _approved(c, issue=413)
    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.stage is Stage.IMPLEMENT and t.park == ""
    assert sess.ended == []
    assert [s[0] for s in _implements(sess)] == [ISSUE]   # 413 was not admitted


# 3. A gate round named by the implement session never parks the task.
@pytest.mark.parametrize("rnd", [1, 2, 3, 40], ids=["below", "at", "past", "far"])
def test_working_report_naming_a_gate_round_does_not_park(tmp_path, monkeypatch, rnd):
    c = _setup(tmp_path, monkeypatch)
    assert c.loop_caps.gate == 2
    wt, sess = _started(c)
    _signal(wt, stage="implement", status="working", loop="gate", round=rnd,
            note="2/4 tickets merged")

    d = deps(sess=sess)
    main.run_pass(c, d)

    t = _task(c)
    assert t.park == "" and t.stage is Stage.IMPLEMENT
    assert sess.ended == []
    assert "parked_question" not in d.notifier.sent
    assert "last_round" not in d.notifier.sent   # the gate cap is not for implement


# 4. A task file from before this change loads.
def test_task_file_with_a_ticket_cursor_loads_and_keeps_its_pull_request(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    make_task(c, issue=ISSUE, stage=Stage.IMPLEMENT)
    make_task(c, issue=413, stage=Stage.PR_OPEN, pr_number=77, slot=1)
    for issue in (ISSUE, 413):
        p = next(__import__("pathlib").Path(c.state_dir).glob(f"task-*-{issue}.json"))
        d = json.loads(p.read_text())
        d["ticket_cursor"], d["ticket_count"] = 2, 4
        p.write_text(json.dumps(d))

    assert {t.issue for t in load_all(c.state_dir)} == {ISSUE, 413}
    main.run_pass(c, deps(sess=FakeSessions(alive={ISSUE, 413})))

    pr = _task(c, 413)
    assert pr is not None and pr.stage is Stage.PR_OPEN and pr.pr_number == 77
    assert _task(c).stage is Stage.IMPLEMENT
    assert {t.issue for t in load_all(c.state_dir)} == {ISSUE, 413}


# 5-8. The implement prompt, rule by rule.
def test_prompt_reads_the_skill_file_over_the_tickets_on_the_existing_branch(
        tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch)
    (para,) = {p for p in _rule(prompt, "implement-spec", "skill.md")}
    assert re.search(r"implement-spec/skill\.md", para)
    assert ".agent/tickets" in para
    assert "read" in para and "follow" in para
    assert "existing" in para and "branch" in para
    assert "agent/412-csv-export" in prompt


def test_prompt_forbids_a_pull_request(tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch)
    (para, *_) = _rule(prompt, "pull request")
    assert re.search(r"(do not|never|don't)[^.]*(create|open)[^.]*pull request", para)
    for banned in ("draft", "new branch"):
        assert banned in para, banned


def test_prompt_reports_blocked_without_subagents_and_implements_nothing(
        tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch)
    (para, *_) = _rule(prompt, "subagent", "blocked")
    assert "isolated" in para
    assert "cannot" in para or "can't" in para or "unable" in para
    assert re.search(r"(implement|write)[^.]*nothing|do not implement|not implement", para)


def test_blocked_report_parks_the_task_with_the_reason(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _started(c)
    _signal(wt, stage="implement", status="blocked",
            note="cannot dispatch an isolated subagent on this runtime")

    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.park == PARK_HUMAN and t.stage is Stage.IMPLEMENT
    assert "isolated subagent" in t.park_note


def test_prompt_keeps_ledger_and_notes_and_reports_merged_counts(tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch)
    (ledger, *_) = _rule(prompt, ".agent/ledger.md", "notes")
    assert "ledger" in ledger
    (report, *_) = _rule(prompt, "tickets merged", "working")
    assert re.search(r"(after|each)[^.]*merge", report)
    assert re.search(r"\bn/m tickets merged|\d+/\d+ tickets merged", report)


# 9. A stopped implement session continues from the ledger.
def test_prompt_continues_from_an_existing_ledger_without_redoing_merged_tickets(
        tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch)
    (para, *_) = _rule(prompt, "ledger", "continue")
    assert re.search(r"(exist|find|found)", para)
    assert re.search(r"(not|never|n't)[^.]*(redo|repeat)", para)
    assert "merged" in para


def test_silent_live_implement_session_parks_and_does_not_fail(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, _ = _started(c)
    _signal(wt, stage="implement", status="working", note="2/4 tickets merged")
    sess = FakeSessions(alive={ISSUE}, idle={ISSUE: 999999.0})

    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.park == PARK_HUMAN and t.stage is Stage.IMPLEMENT


def _dead_after_two_merges(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, _ = _started(c)
    (wt / ".agent" / "ledger.md").write_text("01 merged\n02 merged\n03 running\n")
    _signal(wt, stage="implement", status="working", note="2/4 tickets merged")
    d = deps(sess=FakeSessions(alive=set()))
    main.run_pass(c, d)
    return c, wt, d


def test_dead_implement_session_fails_the_task_as_resumable(tmp_path, monkeypatch):
    c, wt, d = _dead_after_two_merges(tmp_path, monkeypatch)

    t = _task(c)
    assert t.stage is Stage.FAILED and t.crashed_stage == "implement"
    assert "session_crashed" in d.notifier.sent
    assert (wt / ".agent" / "ledger.md").exists()
    assert d.sessions.spawned == []     # no automatic respawn


def test_operator_resume_starts_a_session_in_the_same_worktree_with_the_prompt(
        tmp_path, monkeypatch):
    c, wt, _ = _dead_after_two_merges(tmp_path, monkeypatch)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", ISSUE, {},
                             "op", 1)
    sess = FakeSessions(alive=set())

    main.run_pass(c, deps(sess=sess))

    (spawn,) = _implements(sess)
    assert _task(c).worktree == str(wt) and _task(c).stage is Stage.IMPLEMENT
    assert (wt / ".agent" / "ledger.md").exists()
    assert _rule(spawn[3], "ledger", "continue")
    assert not re.search(r"ticket\s+\d+\s+of\s+\d+", spawn[3].lower())
    assert ".agent/tickets/0" not in spawn[3]


# Requirement 13, scenario "a quota error mid-implement parks and resumes":
# driven through passes, with the console's task view as the observer.
def _progress_shown(c):
    from fastapi.testclient import TestClient
    from tests.test_web_implement_progress import _NoGithub, _NoSessions
    from tests.webfakes import HEADERS
    from web.app import create_app
    from web.sources import Sources
    with TestClient(create_app(c, Sources(c, _NoSessions(), _NoGithub()))) as client:
        r = client.get(f"/api/task/portfolio_eval/{ISSUE}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["implement_progress"]


def test_parked_implement_task_resumes_its_session_and_keeps_its_progress(
        tmp_path, monkeypatch):
    from tests.test_main import LiveUntilEnded
    c = _setup(tmp_path, monkeypatch)
    wt, _ = _started(c)
    _signal(wt, stage="implement", status="working", note="2/4 tickets merged")
    sess = LiveUntilEnded(alive={ISSUE}, idle={ISSUE: 999999.0})
    main.run_pass(c, deps(sess=sess))               # silent live session: parked
    assert _task(c).park == PARK_HUMAN and sess.ended == [ISSUE]
    assert _progress_shown(c) == "2/4 tickets merged"

    sess.idle = {}
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", ISSUE,
                             {"text": "the quota is back, continue"}, "op", 1)
    main.run_pass(c, deps(sess=sess))               # the operator's reply resumes it
    main.run_pass(c, deps(sess=sess))               # and nothing else happens

    t = _task(c)
    assert (t.stage, t.park) == (Stage.IMPLEMENT, "")
    assert [r[0] for r in sess.resumed] == [ISSUE], "one resume of the same session"
    assert "the quota is back, continue" in sess.resumed[0][1]
    assert _implements(sess) == [], "no second implement session"
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"
    assert _progress_shown(c) == "2/4 tickets merged"


def test_respawn_after_a_crash_keeps_the_progress_and_a_new_stage_drops_it(
        tmp_path, monkeypatch):
    c, wt, _ = _dead_after_two_merges(tmp_path, monkeypatch)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", ISSUE, {},
                             "op", 1)
    sess = FakeSessions(alive=set())
    main.run_pass(c, deps(sess=sess))               # fresh implement session
    assert len(_implements(sess)) == 1
    assert _progress_shown(c) == "2/4 tickets merged"

    _signal(wt, stage="implement", status="done", note="4/4 tickets merged")
    sess.alive_set = {ISSUE}
    main.run_pass(c, deps(sess=sess))               # review starts
    assert _task(c).stage is Stage.REVIEW
    assert "note" not in json.loads((wt / ".agent" / "stage.json").read_text())


def test_blocked_reason_is_not_kept_as_progress_after_the_reply(tmp_path, monkeypatch):
    from tests.test_main import LiveUntilEnded
    c = _setup(tmp_path, monkeypatch)
    wt, _ = _started(c)
    _signal(wt, stage="implement", status="blocked", note="no subagent tool")
    sess = LiveUntilEnded(alive={ISSUE})
    main.run_pass(c, deps(sess=sess))
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", ISSUE,
                             {"text": "fixed"}, "op", 1)
    main.run_pass(c, deps(sess=sess))
    assert len(sess.resumed) == 1 and _progress_shown(c) is None
