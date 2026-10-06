"""The gate skip, beyond the acceptance tests: what decides it, what it still
does, and what can never trigger it. Same harness as
tests/test_gate_skip_acceptance.py."""
import json
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher import spec_publish
from dispatcher.artifacts import count_open_questions
from dispatcher.machine import SetTaskStage, StartTicket, next_actions
from dispatcher.state import Stage, StageSignal, TaskState, read_stage_signal

from tests.test_gate_skip_acceptance import (BRANCH, FOLDER, ISSUE,
                                             NONE_SUMMARY, ONE_SUMMARY, SUMMARY,
                                             _assert_waits, _git, _implements,
                                             _pass, _ready, _setup, _task,
                                             _task_at_ready, _wait_case)
from tests.test_main import FakeGitHub, deps, write_tickets


def test_track_named_in_the_ready_report_does_not_count(tmp_path, monkeypatch):
    _wait_case(tmp_path, monkeypatch, track="standard", report={"track": "trivial"})


def test_skip_publishes_the_spec_folder_and_asks_nobody(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path)
    gh = FakeGitHub()
    d = deps(gh=gh, sess=sess)
    main.run_pass(c, d)
    pushed = _git(tmp_path / "origin.git", "ls-tree", "-r", "--name-only", BRANCH)
    assert f"{FOLDER}/design.md" in pushed.splitlines()
    assert ".agent" not in pushed
    assert gh.comments == []
    assert d.notifier.sent == ["implement_started"]


def test_push_failure_does_not_block_the_skip_and_is_named(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path)
    monkeypatch.setattr(main.spec_publish, "ensure_published",
                        lambda **k: spec_publish.PublishResult(error="git push failed: auth"))
    d = _pass(c, sess)
    assert _task(c).stage is Stage.IMPLEMENT
    ((template, ctx),) = d.notifier.contexts
    assert template == "implement_started" and "local only" in json.dumps(ctx)


def _waited_once(tmp_path, monkeypatch):
    """A gate-free task whose first plan had one open question: it waits."""
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, summary=ONE_SUMMARY,
                              report={"open_questions": 1})
    _assert_waits(c, sess, _pass(c, sess))
    return c, wt, sess


def test_feedback_round_at_the_gate_never_skips(tmp_path, monkeypatch):
    c, wt, sess = _waited_once(tmp_path, monkeypatch)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "working", "note": "applying feedback"}))
    _pass(c, sess)
    (wt / SUMMARY).write_text(NONE_SUMMARY)
    _ready(wt)                                  # nothing open any more
    _assert_waits(c, sess, _pass(c, sess))


def test_task_that_waited_never_skips_after_a_respawn(tmp_path, monkeypatch):
    """The plan session died at the gate and a fresh one, in the plan stage
    again, reports a plan with nothing open: the operator is still asked."""
    c, wt, sess = _waited_once(tmp_path, monkeypatch)
    sess.alive_set = set()
    _pass(c, sess)                              # found dead: respawned
    assert _task(c).stage is Stage.PLAN
    (wt / SUMMARY).write_text(NONE_SUMMARY)
    _ready(wt)
    sess.alive_set = {ISSUE}
    _assert_waits(c, sess, _pass(c, sess))


def test_gate_free_task_that_waited_starts_implement_on_approval(tmp_path, monkeypatch):
    c, wt, sess = _waited_once(tmp_path, monkeypatch)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done"}))
    _pass(c, sess)
    assert _task(c).stage is Stage.IMPLEMENT
    assert [s[0] for s in _implements(sess)] == [ISSUE]


def _summary(section):
    return f"# Plan review\n\n## Tickets\n\n- 01 A\n\n{section}\n## Corrections\n\nNone.\n"


@pytest.mark.parametrize("section, count", [
    ("## Open questions\n\nNone.\n", 0),
    ("## Open questions\n\n- One? Recommend a.\n", 1),
    ("## Open questions\n\n- One?\n  Recommend a: reason.\n\n- Two?\n", 2),
    ("## Open questions\n\n1. One?\n2. Two?\n3. Three?\n", 3),
    ("", None),                                              # no section
    ("## Open questions\n\n", None),                         # empty section
    ("## Open questions\n\nNone\n", None),                   # not the prescribed word
    ("## Open questions\n\nNone.\n\nBut see ticket 02.\n", None),
    ("## Open questions\n\nWhich wording?\n", None),         # prose, no list
    ("## Open questions\n\n- One?\n\n### More\n\n- Two?\n", None),
    ("## Open questions\n\nNone.\n\n## Open questions\n\n- One?\n", None),
], ids=["none", "one", "two-with-continuation", "numbered", "no-section",
        "empty", "none-without-period", "none-plus-prose", "prose",
        "sub-heading", "two-sections"])
def test_count_open_questions(tmp_path, section, count):
    p = tmp_path / "plan-review.md"
    p.write_text(_summary(section))
    assert count_open_questions(p) == count


def test_count_open_questions_of_an_unreadable_summary_is_unknown(tmp_path):
    assert count_open_questions(tmp_path / "missing.md") is None
    assert count_open_questions(tmp_path) is None            # a directory
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe## Open questions\n\nNone.\n")
    assert count_open_questions(tmp_path / "bin.md") is None


@pytest.mark.parametrize("raw, parsed", [(0, 0), (3, 3), (-1, None), ("0", None),
                                         (0.0, None), (True, None), (None, None),
                                         ([0], None)])
def test_bad_open_questions_does_not_make_the_signal_unreadable(tmp_path, raw, parsed):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "open_questions": raw}))
    sig = read_stage_signal(tmp_path)
    assert sig.status == "awaiting-review" and sig.open_questions == parsed


def test_task_file_at_the_gate_from_before_the_gated_flag_never_skips(tmp_path):
    """A task saved at the gate before `gated` existed loads with it false:
    the stage alone keeps its next ready report at the gate."""
    (tmp_path / ".agent").mkdir()
    write_tickets(tmp_path, 1)
    (tmp_path / SUMMARY).write_text(NONE_SUMMARY)
    t = TaskState(issue=ISSUE, target="portfolio_eval", slot=0, branch=BRANCH,
                  stage=Stage.AWAITING_PLAN_REVIEW, worktree=str(tmp_path),
                  title="t", updated_at="2026-10-13T00:00:00+00:00", track="trivial")
    ready = StageSignal("plan", "awaiting-review", artifact=SUMMARY, open_questions=0)
    acts = next_actions(t, ready, True, gate_free=frozenset({"trivial"}))
    assert SetTaskStage(Stage.AWAITING_PLAN_REVIEW, artifact=SUMMARY) in acts
    assert not any(isinstance(a, StartTicket) for a in acts)
    skipped = next_actions(dc_replace(t, stage=Stage.PLAN), ready, True,
                           gate_free=frozenset({"trivial"}))
    assert any(isinstance(a, StartTicket) for a in skipped)
