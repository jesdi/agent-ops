"""The gate skip, beyond the acceptance tests: what decides it, what it still
does, and what can never trigger it. Same harness as
tests/test_gate_skip_acceptance.py."""
import json
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher import intents as intents_mod
from dispatcher import spec_publish
from dispatcher.artifacts import REVIEW_PAGE_MARKER, REVIEW_PAGE_MAX_BYTES, count_open_questions
from dispatcher.machine import SetTaskStage, SpawnStage, next_actions
from dispatcher.state import Stage, StageSignal, TaskState, read_stage_signal, save

from tests.test_gate_skip_acceptance import (BRANCH, FOLDER, ISSUE,
                                             NONE_SUMMARY, ONE_SUMMARY, SUMMARY, _page,
                                             _assert_waits, _git, _implements,
                                             _pass, _ready, _setup, _task,
                                             _task_at_ready, _wait_case)
from tests.test_main import FakeGitHub, LiveUntilEnded, deps, write_tickets


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


def test_count_open_questions_of_an_unreadable_page_is_unknown(tmp_path):
    assert count_open_questions(tmp_path / "missing.html") is None
    assert count_open_questions(tmp_path) is None            # a directory
    (tmp_path / "bin.html").write_bytes(b"\xff\xfe" + REVIEW_PAGE_MARKER.encode())
    assert count_open_questions(tmp_path / "bin.html") == 0  # the marker still shows


@pytest.mark.parametrize("raw, parsed", [(0, 0), (3, 3), (-1, None), ("0", None),
                                         (0.0, None), (True, None), (False, None),
                                         (None, None),
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
    assert SpawnStage(Stage.IMPLEMENT, tickets=1) not in acts
    skipped = next_actions(dc_replace(t, stage=Stage.PLAN), ready, True,
                           gate_free=frozenset({"trivial"}))
    assert SpawnStage(Stage.IMPLEMENT, tickets=1) in skipped


# --- fix round 1 --------------------------------------------------------------

def test_false_open_question_count_waits(tmp_path, monkeypatch):
    """JSON `false` equals 0 in Python: it is not a count."""
    _wait_case(tmp_path, monkeypatch, report={"open_questions": False})


def _assert_no_implement(c, sess):
    assert _task(c).stage.value == "awaiting-plan-review"
    assert _implements(sess) == []


def test_count_is_checked_against_the_summary_the_operator_would_see(tmp_path, monkeypatch):
    """The prescribed path says `None.`, but the ready report names another
    file, with a question in it: that is what the gate would show."""
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, report={"artifact": ".agent/other.md"})
    (wt / ".agent" / "other.md").write_text(ONE_SUMMARY)
    _pass(c, sess)
    _assert_no_implement(c, sess)


def test_ready_report_without_an_artifact_waits_with_the_review_page(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, report={"artifact": ""})
    _pass(c, sess)
    _assert_no_implement(c, sess)


@pytest.mark.parametrize("artifact", ["specs", "/nonexistent/review.html"])
def test_ready_report_with_another_artifact_is_bounced(tmp_path, monkeypatch, artifact):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, report={"artifact": artifact})
    _pass(c, sess)
    assert _task(c).stage is Stage.PLAN and _implements(sess) == []


def test_questionnaire_raised_by_the_plan_session_counts(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, _ = _task_at_ready(c, tmp_path)
    sess = LiveUntilEnded(alive={ISSUE})
    (wt / ".agent" / "questions.md").write_text("# Q\n\n1. Which wording?\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-answers",
         "artifact": ".agent/questions.md"}))
    _pass(c, sess)                              # parks for the answer
    assert _task(c).park and _task(c).asked is True
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", ISSUE,
                             {"text": "Export CSV"}, actor="op", epoch_ms=1)
    _pass(c, sess)                              # resumed with the answer
    assert _task(c).park == "" and len(sess.resumed) == 1
    _ready(wt)                                  # nothing open any more
    _assert_waits(c, sess, _pass(c, sess))


def test_blocked_park_in_the_plan_stage_is_not_a_questionnaire(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "blocked", "note": "spec contradicts the code"}))
    _pass(c, sess)
    assert _task(c).park and _task(c).asked is False


def test_old_gate_task_file_never_skips_after_its_session_dies(tmp_path, monkeypatch):
    """A task file saved at the gate before `gated` existed has no such key.
    Its dead session is respawned into the plan stage; the fresh session's
    ready report with nothing open still waits."""
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path)
    save(c.state_dir, dc_replace(_task(c), stage=Stage.AWAITING_PLAN_REVIEW))
    (f,) = [p for p in (tmp_path / "state").glob("task-*.json")]
    doc = json.loads(f.read_text())
    del doc["gated"]
    f.write_text(json.dumps(doc))
    sess.alive_set = set()
    _pass(c, sess)                              # found dead: respawned
    assert _task(c).stage is Stage.PLAN
    _ready(wt)
    sess.alive_set = {ISSUE}
    _assert_waits(c, sess, _pass(c, sess))


def test_summary_is_not_read_when_another_condition_already_fails(tmp_path, monkeypatch):
    def boom(_path):
        raise AssertionError("summary read for a task that cannot skip")
    monkeypatch.setattr("dispatcher.machine.count_open_questions", boom)
    _wait_case(tmp_path, monkeypatch, track="standard")


def test_page_above_the_size_cap_cannot_be_counted(tmp_path):
    p = tmp_path / "review.html"
    p.write_text(NONE_SUMMARY + "x" * REVIEW_PAGE_MAX_BYTES)
    assert count_open_questions(p) is None
    p.write_text(NONE_SUMMARY + "x" * (REVIEW_PAGE_MAX_BYTES - len(NONE_SUMMARY)))
    assert p.stat().st_size == REVIEW_PAGE_MAX_BYTES and count_open_questions(p) == 0


def test_every_question_block_is_counted(tmp_path):
    p = tmp_path / "review.html"
    p.write_text(_page(2000))
    assert count_open_questions(p) == 2000


@pytest.mark.parametrize("body, count", [
    ('<div data-q="a"></div><div data-q="b-2"></div>', 2),
    ('<div data-q=""></div>', 0),                 # no id
    ('<div data-qx="a"></div>', 0),               # another attribute
    ('<div data-q="A B"></div>', 0),              # not an id
])
def test_count_reads_the_question_ids_only(tmp_path, body, count):
    p = tmp_path / "review.html"
    p.write_text(REVIEW_PAGE_MARKER + body)
    assert count_open_questions(p) == count


def test_page_without_the_marker_cannot_be_counted(tmp_path):
    p = tmp_path / "review.html"
    p.write_text('<div data-q="a"></div>')
    assert count_open_questions(p) is None


def test_failed_implement_launch_on_the_skip_path_resumes_into_implement(
        tmp_path, monkeypatch):
    """The skip was decided in the pass whose launch failed: Resume starts
    implement, and nobody is asked for an approval the task never needed."""
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path)
    sess.spawn_raises = {ISSUE}
    _pass(c, sess)
    failed = _task(c)
    assert (failed.stage, failed.crashed_stage) == (Stage.FAILED, "implement")
    assert (failed.gated, failed.asked) == (False, False)

    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", ISSUE, {}, "op", 1)
    sess = LiveUntilEnded()
    d = _pass(c, sess)
    d2 = _pass(c, sess)
    t = _task(c)
    assert [s[0] for s in _implements(sess)] == [ISSUE]
    assert (t.stage, t.gated, t.asked, t.ticket_count) == (Stage.IMPLEMENT, False, False, 1)
    assert not [n for n in d.notifier.sent + d2.notifier.sent if "review" in n]


def test_fifo_or_symlink_as_the_page_cannot_be_counted(tmp_path):
    import os
    import threading
    fifo = tmp_path / "fifo.html"
    os.mkfifo(fifo)
    out = []
    th = threading.Thread(target=lambda: out.append(count_open_questions(fifo)), daemon=True)
    th.start()
    th.join(2)
    assert not th.is_alive() and out == [None]
    (tmp_path / "real.html").write_text(NONE_SUMMARY)
    (tmp_path / "link.html").symlink_to(tmp_path / "real.html")
    assert count_open_questions(tmp_path / "link.html") is None


def test_nul_character_in_the_ready_reports_artifact_waits_at_the_gate(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _task_at_ready(c, tmp_path, report={"artifact": ".agent/plan\u0000.md"})
    _pass(c, sess)
    assert _task(c).stage is Stage.PLAN and _implements(sess) == []




def _skip_inputs(tmp_path, **kw):
    """A task and ready report that skip the gate; `kw` spoils one condition."""
    from dispatcher.machine import _skips_gate
    (tmp_path / ".agent").mkdir(exist_ok=True)
    (tmp_path / SUMMARY).write_text(NONE_SUMMARY)
    t = TaskState(issue=ISSUE, target="portfolio_eval", slot=0, branch=BRANCH,
                  stage=Stage.PLAN, worktree=str(tmp_path), title="t",
                  updated_at="2026-10-13T00:00:00+00:00", track="trivial")
    sig = StageSignal("plan", "awaiting-review", artifact=SUMMARY, open_questions=0)
    return _skips_gate, dc_replace(t, **kw.pop("task", {})), dc_replace(sig, **kw)


def test_skips_gate_when_every_condition_holds(tmp_path):
    skips, t, sig = _skip_inputs(tmp_path)
    assert skips(t, sig, frozenset({"trivial"}))


@pytest.mark.parametrize("spoil", [
    {"task": {"gated": True}}, {"task": {"track": "standard"}},
    {"task": {"asked": True}}, {"open_questions": 1},
    {"artifact": ".agent/other.html"}, {"artifact": ".agent/plan\u0000.html"},
], ids=["gated", "track", "asked", "reported", "artifact", "nul-artifact"])
def test_each_spoiled_condition_keeps_the_gate(tmp_path, spoil):
    skips, t, sig = _skip_inputs(tmp_path, **spoil)
    assert not skips(t, sig, frozenset({"trivial"}))


def test_page_with_a_question_block_keeps_the_gate(tmp_path):
    skips, t, sig = _skip_inputs(tmp_path)
    (tmp_path / SUMMARY).write_text(ONE_SUMMARY)
    assert not skips(t, sig, frozenset({"trivial"}))
