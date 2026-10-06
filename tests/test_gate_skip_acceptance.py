"""Acceptance tests for ticket 04: the plan review gate is skipped only when
the track is gate-free, no questionnaire was asked and stage 2 reported no
open question.

Spec: specs/2026-10-06-openspec-pipeline/spec.md, requirement 9 and its
scenarios; design.md (the ready report, the cross-checked open-question
count, `asked`). Black-box through dispatcher.main.run_pass over the fakes of
tests/test_main.py, with a real git repo (bare origin) behind the worktree.
The publish backstop is not asserted: neither the ticket nor the spec asks
for it on the skip path.
"""
import json
import re
import subprocess
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher.models import parse_policy
from dispatcher.state import Stage, load, save

from tests.test_main import (FakeSessions, cfg, deps, make_task, patch_usage,
                             patch_workspace, write_tickets, GOOD_TICKET)

ISSUE = 415
BRANCH = "agent/415-export-label"
FOLDER = "specs/2026-10-13-export-label"
SPEC = f"{FOLDER}/spec.md"
SUMMARY = ".agent/plan-review.md"
GATE = "awaiting-plan-review"
TITLE = "Fix a typo in the export button label"

NONE_SUMMARY = ("# Plan review: Fix a typo\n\n## Tickets\n\n- 01 Fix label, "
                "blocked by none, seam ExportButton\n\n## Open questions\n\n"
                "None.\n\n## Corrections\n\nNone.\n")
ONE_SUMMARY = NONE_SUMMARY.replace(
    "None.\n\n## Corrections",
    "- Which label wording? Recommend \"Export CSV\": it matches the menu.\n\n"
    "## Corrections")
assert NONE_SUMMARY != ONE_SUMMARY


def _setup(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)

    def track(name, model, **kw):
        m = [model]
        return {"when": f"The {name} track.", "spec": m, "plan": m,
                "implement": m, "review": m, **kw}
    policy = parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {"security": track("security", "claude-opus-5"),
                   "standard": track("standard", "claude-sonnet-5"),
                   "trivial": track("trivial", "claude-sonnet-5",
                                    plan_review=False)}})
    return dc_replace(cfg(tmp_path), models=policy)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _ready(wt, **kw):
    sig = {"stage": "plan", "status": "awaiting-review", "note": "plan ready",
           "artifact": SUMMARY, "open_questions": 0, **kw}
    for k in [k for k, v in sig.items() if v is _MISSING]:
        del sig[k]
    (wt / ".agent" / "stage.json").write_text(json.dumps(sig))


_MISSING = object()


def _task_at_ready(c, tmp_path, *, track="trivial", tickets=1, summary=NONE_SUMMARY,
                   report=None, **kw):
    """Issue 415 with folder, tickets and summary, a ready report written, and
    a live plan session. Returns (worktree, sessions)."""
    wt = make_task(c, issue=ISSUE, stage=Stage.PLAN, spec_path=SPEC,
                   track=track, **kw)
    save(c.state_dir, dc_replace(load(c.state_dir, "portfolio_eval", ISSUE),
                                 branch=BRANCH, title=TITLE))
    f = wt / FOLDER
    f.mkdir(parents=True, exist_ok=True)
    (f / "proposal.md").write_text("# Proposal\n\n" + "w " * 400)
    (f / "spec.md").write_text("# Label: Spec\n\n" + "x " * 400)
    (f / "design.md").write_text("# Label: Design\n\n" + "d " * 400)
    write_tickets(wt, tickets)
    if summary is not None:
        (wt / SUMMARY).write_text(summary)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", BRANCH, str(wt)], check=True,
                   capture_output=True)
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("seed\n")
    _git(wt, "add", "README.md")
    _git(wt, "commit", "-m", "init")
    _git(wt, "remote", "add", "origin", str(origin))
    _git(wt, "push", "origin", BRANCH)
    _ready(wt, **(report or {}))
    return wt, FakeSessions(alive={ISSUE})


def _pass(c, sess):
    d = deps(sess=sess)
    main.run_pass(c, d)
    return d


def _implements(sess):
    return [s for s in sess.spawned if s[1] == "implement"]


def _task(c):
    return load(c.state_dir, "portfolio_eval", ISSUE)


def _assert_waits(c, sess, d):
    t = _task(c)
    assert t.stage.value == GATE
    assert t.operator_request is not None
    assert t.operator_request.kind == "plan-approval"
    assert t.operator_request.path == SUMMARY
    assert _implements(sess) == []
    assert len([n for n in d.notifier.sent if "review" in n]) == 1


def _wait_case(tmp_path, monkeypatch, **kw):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, **kw)
    d = _pass(c, sess)
    _assert_waits(c, sess, d)


# 1. The skip.
def test_trivial_task_with_nothing_open_goes_straight_to_implement(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path)

    d = _pass(c, sess)

    t = _task(c)
    assert t.stage is Stage.IMPLEMENT
    assert t.operator_request is None
    assert [s[0] for s in _implements(sess)] == [ISSUE]
    assert not any("review" in n for n in d.notifier.sent)
    assert (wt / FOLDER / "design.md").is_file()
    assert (wt / ".agent" / "tickets" / "01-t1.md").is_file()


# 2-4. Each other condition on its own makes the task wait.
def test_trivial_task_with_one_open_question_waits(tmp_path, monkeypatch):
    _wait_case(tmp_path, monkeypatch, summary=ONE_SUMMARY,
               report={"open_questions": 1})


def test_trivial_task_that_needed_a_questionnaire_waits(tmp_path, monkeypatch):
    _wait_case(tmp_path, monkeypatch, asked=True)


def test_standard_task_with_nothing_open_still_waits(tmp_path, monkeypatch):
    _wait_case(tmp_path, monkeypatch, track="standard")


# 5. An open-question count that is not a non-negative integer fails closed.
@pytest.mark.parametrize("count", [_MISSING, -1, "0", 0.0, True, None, [0]],
                         ids=["missing", "negative", "string", "float", "bool",
                              "null", "list"])
def test_bad_open_question_count_waits(tmp_path, monkeypatch, count):
    _wait_case(tmp_path, monkeypatch, report={"open_questions": count})


# 6. The count is cross-checked against the summary.
def test_zero_reported_but_summary_lists_one_question_waits(tmp_path, monkeypatch):
    _wait_case(tmp_path, monkeypatch, summary=ONE_SUMMARY,
               report={"open_questions": 0})


def test_one_reported_but_summary_says_none_waits(tmp_path, monkeypatch):
    _wait_case(tmp_path, monkeypatch, summary=NONE_SUMMARY,
               report={"open_questions": 1})


def test_missing_summary_file_waits(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, summary=None)
    d = _pass(c, sess)
    t = _task(c)
    assert t.stage.value != "implement"
    assert _implements(sess) == []
    assert not any(n for n in d.notifier.sent if "implement" in n)


# 7. A skipped gate still requires a valid ticket set.
def test_skip_path_with_a_ticket_gap_is_bounced(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, tickets=2)
    (wt / ".agent" / "tickets" / "02-t2.md").rename(
        wt / ".agent" / "tickets" / "04-t4.md")

    d = _pass(c, sess)

    (resume,) = sess.resumed
    assert "ticket numbers must be contiguous from 01" in resume[1]
    assert _implements(sess) == []
    t = _task(c)
    assert t.stage is Stage.PLAN
    assert t.operator_request is None
    assert not any("review" in n for n in d.notifier.sent)


# 8. The plan prompt asks for the count.
def test_plan_prompt_tells_the_session_to_report_open_questions(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC, track="standard")
    f = wt / FOLDER
    f.mkdir(parents=True, exist_ok=True)
    (f / "proposal.md").write_text("# Proposal\n\n" + "w " * 400)
    (f / "spec.md").write_text("# Label: Spec\n\n## Requirements\n\n" + "x " * 400
                               + "\n\n## Scenarios\n\n" + "y " * 400)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": SPEC,
         "track": "standard", "note": "stage 1 written"}))
    sess = FakeSessions(alive={ISSUE})
    main.run_pass(c, deps(sess=sess))
    (spawn,) = [s for s in sess.spawned if s[1] == "plan"]
    p = spawn[3]

    # The signal line of the ready report: from its `awaiting-review` JSON to
    # the next signal bullet.
    m = re.search(r'- `\{"stage": "plan", "status": "awaiting-review"', p)
    assert m, "prompt has no awaiting-review signal line"
    line = re.split(r"\n- `\{", p[m.start():], maxsplit=1)[0]
    assert "open_questions" in line
