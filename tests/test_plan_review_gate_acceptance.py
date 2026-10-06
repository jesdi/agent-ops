"""Acceptance tests for ticket 03: the plan session checks stage 1, writes
stage 2 and waits at the plan review gate.

Spec: specs/2026-10-06-openspec-pipeline/spec.md, requirements 5-8 and 10 and
their scenarios; design.md (Decisions, Data model, Seams). Black-box, through
dispatcher.main.run_pass over the fakes of tests/test_main.py, with a real git
repo (bare origin) behind the task's worktree so the publish step is real.
The console test goes through GET /api/task/{target}/{issue}/request.
"""
import json
import re
import subprocess
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import pytest

import dispatcher.main as main
from dispatcher import intents as intents_mod
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import NO_SLOT, PARK_HUMAN, PARK_REVIEW, Stage, load, save

from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, cfg,
                             deps, make_task, patch_usage, patch_workspace,
                             write_tickets, GOOD_TICKET)

ISSUE = 412
BRANCH = "agent/412-csv-export"
FOLDER = "specs/2026-10-12-csv-export"
SPEC = f"{FOLDER}/spec.md"
SUMMARY = ".agent/plan-review.md"
FOLDER_URL = f"https://github.com/jesdi/portfolio_eval/tree/{BRANCH}/{FOLDER}"
GATE = "awaiting-plan-review"

SUMMARY_TEXT = (
    "# Plan review: Export a portfolio as CSV\n\n## Tickets\n\n"
    "| Number | Title | Blocked by | Seam |\n|---|---|---|---|\n"
    "| 01 | Export endpoint | None | GET /api/export |\n"
    "| 02 | Owner check | 01 | GET /api/export |\n"
    "| 03 | Column order | 01 | CsvWriter |\n"
    "| 04 | Console button | 02, 03 | ExportButton |\n\n"
    "## Open questions\n\nNone.\n\n## Corrections\n\nNone.\n")


def _policy():
    def track(name, model):
        m = [model]
        return {"when": f"The {name} track.", "spec": m, "plan": m,
                "implement": m, "review": m}
    return parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {"security": track("security", "claude-opus-5"),
                   "standard": track("standard", "claude-sonnet-5"),
                   "trivial": track("trivial", "claude-sonnet-5")}})


def _cfg(tmp_path, **kw):
    return dc_replace(cfg(tmp_path), models=_policy(), **kw)


def _setup(tmp_path, monkeypatch, **kw):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return _cfg(tmp_path, **kw)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _signal(wt, **kw):
    (wt / ".agent" / "stage.json").write_text(json.dumps(kw))


def _ready(wt, **kw):
    _signal(wt, **{"stage": "plan", "status": "awaiting-review",
                   "note": "plan ready", "artifact": SUMMARY,
                   "open_questions": 0, **kw})


def _done(wt, track="standard"):
    _signal(wt, stage="plan", status="done", note="approved",
            artifact=".agent/tickets", track=track)


def _folder_files(wt, design=True):
    f = wt / FOLDER
    f.mkdir(parents=True, exist_ok=True)
    (f / "proposal.md").write_text("# Proposal\n\n## Why\n\n" + "w " * 400)
    (f / "spec.md").write_text("# Export a portfolio as CSV: Spec\n\n"
                               "## Requirements\n\n" + "x " * 400
                               + "\n\n## Scenarios\n\n" + "y " * 400)
    if design:
        (f / "design.md").write_text("# Export: Design\n\n## Decisions\n\n"
                                     + "d " * 400)


def _plan_task(c, tmp_path, stage=Stage.PLAN, *, tickets=4, git=True,
               origin_ok=True, **kw):
    """Issue 412 with its folder, tickets and summary in the worktree; a real
    git repo on the task branch with a bare origin when `git` is true."""
    wt = make_task(c, issue=ISSUE, stage=stage, spec_path=SPEC, **kw)
    save(c.state_dir, dc_replace(load(c.state_dir, "portfolio_eval", ISSUE),
                                 branch=BRANCH, title="Export a portfolio as CSV"))
    _folder_files(wt)
    write_tickets(wt, tickets)
    (wt / SUMMARY).write_text(SUMMARY_TEXT)
    if git:
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
        _git(wt, "remote", "add", "origin",
             str(origin if origin_ok else tmp_path / "nowhere.git"))
        if origin_ok:
            _git(wt, "push", "origin", BRANCH)
    return wt


def _blob(tmp_path, path):
    out = subprocess.run(["git", "-C", str(tmp_path / "origin.git"), "show",
                          f"{BRANCH}:{path}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def _everything(d):
    return json.dumps([d.notifier.contexts, d.github.comments]).lower()


def _first_gate(c, tmp_path, **kw):
    wt = _plan_task(c, tmp_path, **kw)
    _ready(wt)
    sess = FakeSessions(alive={ISSUE})
    d = deps(sess=sess)
    main.run_pass(c, d)
    return wt, sess, d


def _task(c):
    return load(c.state_dir, "portfolio_eval", ISSUE)


# 1. The task waits at the plan review gate.
def test_ready_report_makes_the_task_wait_with_a_folder_link(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess, d = _first_gate(c, tmp_path)

    t = _task(c)
    assert t.stage.value == GATE
    assert t.park == ""
    assert [s for s in sess.spawned if s[1] == "implement"] == []
    assert t.operator_request is not None
    assert t.operator_request.kind == "plan-approval"
    assert t.operator_request.path == SUMMARY
    seen = _everything(d)
    assert FOLDER_URL.lower() in seen
    assert (FOLDER_URL + "/spec.md").lower() not in seen


def test_console_shows_the_summary_as_the_task_request(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS
    from web.app import create_app
    from web.sources import Sources
    c = _setup(tmp_path, monkeypatch)
    _first_gate(c, tmp_path)

    with TestClient(create_app(c, Sources(c, sessions=None, github=None))) as client:
        r = client.get(f"/api/task/portfolio_eval/{ISSUE}/request",
                       headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "plan-approval"
    assert body["content"]["kind"] == "readable"
    assert body["content"]["path"] == SUMMARY
    assert body["content"]["text"] == SUMMARY_TEXT


# 2. The dispatcher commits and pushes the whole folder at the gate.
def test_gate_commits_and_pushes_the_whole_folder_but_not_the_tickets(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path)
    # The session committed stage 1, then left a correction and design.md.
    _git(wt, "add", f"{FOLDER}/proposal.md", SPEC)
    _git(wt, "commit", "-m", "stage 1")
    _git(wt, "push", "origin", BRANCH)
    (wt / SPEC).write_text((wt / SPEC).read_text() + "\nCORRECTION-MARKER\n")
    _ready(wt)
    d = deps(sess=FakeSessions(alive={ISSUE}))

    main.run_pass(c, d)

    assert _task(c).stage.value == GATE
    for name in ("proposal.md", "spec.md", "design.md"):
        assert _blob(tmp_path, f"{FOLDER}/{name}") is not None, name
    assert "CORRECTION-MARKER" in _blob(tmp_path, SPEC)
    assert _blob(tmp_path, ".agent/plan-review.md") is None
    assert _blob(tmp_path, ".agent/tickets/01-t1.md") is None


def test_push_failure_does_not_block_the_gate_and_is_named(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, origin_ok=False)
    _ready(wt)
    d = deps(sess=FakeSessions(alive={ISSUE}))

    main.run_pass(c, d)

    assert _task(c).stage.value == GATE
    notified = json.dumps([x for x in d.notifier.contexts]).lower()
    assert "push" in notified


# 3. Approval starts implement.
def test_approval_starts_one_implement_session_on_the_standard_track(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess, d = _first_gate(c, tmp_path)
    assert _task(c).stage.value == GATE   # the task did wait first
    _done(wt)

    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.stage is Stage.IMPLEMENT
    assert t.track == "standard"
    impl = [s for s in sess.spawned if s[1] == "implement"]
    assert len(impl) == 1
    assert impl[0][0] == ISSUE and impl[0][2].endswith("claude-sonnet-5")


# 4. Approval names another track.
def test_approval_naming_security_runs_implement_on_the_security_entries(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess, d = _first_gate(c, tmp_path)
    assert _task(c).stage.value == GATE   # the task did wait first
    _done(wt, track="security")

    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.stage is Stage.IMPLEMENT
    assert t.track == "security"
    (impl,) = [s for s in sess.spawned if s[1] == "implement"]
    assert impl[2].endswith("claude-opus-5")


def test_approval_naming_an_unconfigured_track_is_bounced_once(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess, d = _first_gate(c, tmp_path)
    assert _task(c).stage.value == GATE   # the task did wait first
    _done(wt, track="fast")

    main.run_pass(c, deps(sess=sess))

    (resume,) = sess.resumed
    assert "security, standard, trivial" in resume[1].replace("'", "")
    assert [s for s in sess.spawned if s[1] == "implement"] == []
    t = _task(c)
    assert t.stage.value == GATE
    assert t.track == "standard"


# 5. A plan done that never waited at the gate is not accepted.
def test_plan_done_without_the_gate_is_resumed_once_then_parks(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)
    _done(wt)
    sess = FakeSessions(alive={ISSUE})

    main.run_pass(c, deps(sess=sess))

    assert [s for s in sess.spawned if s[1] == "implement"] == []
    (resume,) = sess.resumed
    assert "review" in resume[1].lower()
    t = _task(c)
    assert t.stage is Stage.PLAN and t.park == ""

    main.run_pass(c, deps(sess=sess))

    assert len(sess.resumed) == 1
    assert [s for s in sess.spawned if s[1] == "implement"] == []
    t = _task(c)
    assert t.stage is Stage.PLAN and t.park == PARK_HUMAN


# 6. An invalid ticket set does not reach the gate.
def test_ticket_gap_resumes_the_session_and_does_not_ask_for_review(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False, tickets=0)
    write_tickets(wt, 2)
    (wt / ".agent" / "tickets" / "04-t4.md").write_text(GOOD_TICKET)
    _ready(wt)
    sess = FakeSessions(alive={ISSUE})
    d = deps(sess=sess)

    main.run_pass(c, d)

    (resume,) = sess.resumed
    assert "ticket numbers must be contiguous from 01" in resume[1]
    t = _task(c)
    assert t.stage is Stage.PLAN
    assert t.operator_request is None
    assert not any("review" in n for n in d.notifier.sent)


# 7. The gate releases its slot after the grace time.
def test_gate_parks_after_the_grace_time_and_a_later_approval_starts_implement(
        tmp_path, monkeypatch):
    c = dc_replace(_setup(tmp_path, monkeypatch), capacity=1)
    wt = _plan_task(c, tmp_path)
    save(c.state_dir, dc_replace(
        _task(c), stage=Stage(GATE),
        updated_at=(datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()))
    _ready(wt)
    gh = FakeGitHub([Candidate(99, "next", "u99")])
    sess = FakeSessions(alive={ISSUE})

    main.run_pass(c, deps(gh, sess))

    t = _task(c)
    assert t.park == PARK_REVIEW
    assert sess.ended[:1] == [ISSUE]
    assert t.slot == NO_SLOT
    assert gh.claimed == [99]

    sess.alive_set.discard(ISSUE)
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", ISSUE,
                             {"text": "approved"}, actor="op", epoch_ms=1)
    main.run_pass(c, deps(FakeGitHub(), sess))
    sess.alive_set.add(ISSUE)
    _done(wt)
    main.run_pass(c, deps(FakeGitHub(), sess))

    assert _task(c).stage is Stage.IMPLEMENT
    assert [s[1] for s in sess.spawned if s[0] == ISSUE
            and s[1] == "implement"] == ["implement"]


def test_null_grace_never_parks_the_gate(tmp_path, monkeypatch):
    c = dc_replace(_setup(tmp_path, monkeypatch, spec_review_grace_minutes=None),
                   capacity=1)
    wt = _plan_task(c, tmp_path)
    save(c.state_dir, dc_replace(
        _task(c), stage=Stage(GATE),
        updated_at=(datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()))
    _ready(wt)
    gh = FakeGitHub([Candidate(99, "next", "u99")])
    sess = FakeSessions(alive={ISSUE})

    main.run_pass(c, deps(gh, sess))

    t = _task(c)
    assert t.park == "" and t.stage.value == GATE
    assert sess.ended == []
    assert gh.claimed == []


# 8. A gate task whose session died gets a fresh plan session.
def test_gate_task_with_a_dead_session_gets_a_fresh_plan_session(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, track="security")
    save(c.state_dir, dc_replace(_task(c), stage=Stage(GATE)))
    _ready(wt)
    sess = FakeSessions(alive=set())

    main.run_pass(c, deps(sess=sess))

    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]
    t = _task(c)
    assert t.stage is not Stage.FAILED
    assert t.spec_path == SPEC
    assert t.track == "security"


# 9-11. The plan prompt.
def _plan_prompt(tmp_path, monkeypatch):
    """The prompt of the plan session a spec `done` report starts."""
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC, track="standard")
    _folder_files(wt, design=False)
    _signal(wt, stage="spec", status="done", artifact=SPEC, track="standard",
            note="stage 1 written")
    sess = FakeSessions(alive={ISSUE})
    main.run_pass(c, deps(sess=sess))
    (spawn,) = [s for s in sess.spawned if s[1] == "plan"]
    return spawn[3].lower()


def test_prompt_has_the_session_check_and_correct_stage_1(tmp_path, monkeypatch):
    p = _plan_prompt(tmp_path, monkeypatch)
    assert "stage 1" in p
    assert "no source" in p
    assert "no scenario" in p
    assert "boundary" in p and "violation scenario" in p
    for invented in ("invented", "price", "policy", "deadline", "permission"):
        assert invented in p, invented
    assert "correct" in p and "spec.md" in p
    assert "every correction" in p and ".agent/plan-review.md" in p


def test_prompt_has_stage_2_and_the_summary_contents(tmp_path, monkeypatch):
    p = _plan_prompt(tmp_path, monkeypatch)
    assert "to-openspec" in p
    assert "stage 2" in p
    assert ".agent/plan-review.md" in p
    for field in ("number", "title", "blocked by", "seam"):
        assert field in p, field
    assert re.search(r"open[- ]questions", p)
    assert "recommendation" in p
    assert "none." in p
    assert "corrections" in p
    assert "awaiting-review" in p


def test_prompt_treats_feedback_as_never_an_approval(tmp_path, monkeypatch):
    p = _plan_prompt(tmp_path, monkeypatch)
    assert "feedback" in p
    assert "never an approval" in p
    assert "push" in p
    assert "ready again" in p


def test_stage_is_never_the_spec_stage_across_a_feedback_round(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess, d = _first_gate(c, tmp_path)
    seen = [_task(c).stage.value]

    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", ISSUE,
                             {"text": "drop the currency column, requirement 3"},
                             actor="op", epoch_ms=1)
    main.run_pass(c, deps(sess=sess))
    seen.append(_task(c).stage.value)
    _signal(wt, stage="plan", status="working", note="applying feedback")
    main.run_pass(c, deps(sess=sess))
    seen.append(_task(c).stage.value)
    _ready(wt)
    d2 = deps(sess=sess)
    main.run_pass(c, d2)
    seen.append(_task(c).stage.value)

    assert seen[0] == GATE and seen[-1] == GATE
    assert not any(s in ("spec", "awaiting-spec-review") for s in seen), seen
    assert [s for s in sess.spawned if s[1] == "implement"] == []
