"""The decisions of the old-flow migration that the locked acceptance file
(tests/test_openspec_migration_acceptance.py) leaves open.

Deleted together with dispatcher/openspec_migration.py after the deploy. Every
state directory is a pytest tmp_path; the command runs in-process.
"""
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import intents, openspec_migration, spec_publish
from dispatcher.convergence import pass_lock
from dispatcher.state import PARK_WAKE, Stage, load, read_stage_signal, save

from tests.test_main import FakeSessions, deps, make_task
from tests.test_openspec_migration_acceptance import (
    DESIGN, QUESTIONNAIRE, TARGET, cfg_, gated_spec, next_pass, old_task,
    parked_spec, pr_open, snapshot, task_file)


def migrate(c, capsys):
    """(exit status, output) of one run."""
    capsys.readouterr()
    status = openspec_migration.main([c.state_dir])
    return status, capsys.readouterr().out


def raw(c, issue):
    return json.loads(task_file(c, issue).read_text())


# All or nothing.
@pytest.mark.parametrize("stage", ["plan", "implement", "review"])
def test_an_undrained_task_refuses_the_whole_run(tmp_path, monkeypatch, capsys, stage):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    old_task(c, 377, stage)
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status != 0
    assert snapshot(c.state_dir) == before
    assert "nothing was changed" in text.lower()
    lines = text.splitlines()
    assert any(ln.startswith("must-drain") and "#377" in ln for ln in lines), text
    assert any(ln.startswith("not converted") and "#370" in ln for ln in lines), text
    assert not any(ln.startswith("converted") for ln in lines), text


@pytest.mark.parametrize("content", ['{"issue": 999, "stage": "sp', "[1, 2]",
                                     '{"issue": 999, "target": "t"}'])
def test_an_unreadable_task_file_refuses_the_whole_run(tmp_path, monkeypatch,
                                                       capsys, content):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    task_file(c, 999).write_text(content)
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status != 0
    assert snapshot(c.state_dir) == before
    assert any(ln.startswith("unreadable") and task_file(c, 999).name in ln
               for ln in text.splitlines()), text
    assert "nothing was changed" in text.lower()


# What a converted task holds.
def test_converted_task_is_queued_as_a_crashed_spec_stage(tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)

    status, text = migrate(c, capsys)

    assert status == 0, text
    d = raw(c, 384)
    assert (d["stage"], d["park"], d["crashed_stage"]) == ("spec", PARK_WAKE, "spec")
    assert d["operator_request"] is None
    assert d["spec_path"] == ""
    assert d["gated"] is False
    assert "ticket_cursor" not in d
    assert d["updated_at"] == "2026-09-20T00:00:00+00:00"   # keeps its place in the queue
    assert any(ln.startswith("converted") and TARGET in ln and "#384" in ln
               for ln in text.splitlines()), text


def test_counters_that_would_bounce_the_fresh_session_are_reset(tmp_path, monkeypatch,
                                                                capsys):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 370, "spec", spec_retries=1, plan_retries=1, review_rounds=2,
             gate_rounds=2, e2e_rounds=3, ci_rounds=3)

    migrate(c, capsys)

    t = load(c.state_dir, TARGET, 370)
    assert (t.spec_retries, t.plan_retries, t.review_rounds, t.gate_rounds,
            t.e2e_rounds, t.ci_rounds) == (0, 0, 0, 0, 0, 0)


# `asked`: true unless the worktree proves that no questionnaire was raised.
def test_asked_is_false_only_when_the_worktree_holds_no_questionnaire(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c, 384)                                   # worktree, no questionnaire
    wt = gated_spec(c, 385)                              # a questionnaire was raised
    (wt / QUESTIONNAIRE).write_text("# Questions\n1. One?\n")
    old_task(c, 386, "awaiting-spec-review", park="awaiting-review",
             worktree=str(tmp_path / "gone"),            # history unknown
             operator_request={"kind": "spec-approval"})
    old_task(c, 387, "awaiting-spec-review", worktree="")

    migrate(c, capsys)

    assert {i: raw(c, i)["asked"] for i in (384, 385, 386, 387)} == {
        384: False, 385: True, 386: True, 387: True}
    assert all(raw(c, i)["gated"] is False for i in (384, 385, 386, 387))


def test_an_answers_request_means_asked_whatever_the_worktree_holds(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 370, "spec", park="parked",
             operator_request={"kind": "answers", "path": ".agent/prototype.html"})

    migrate(c, capsys)

    assert raw(c, 370)["asked"] is True


# A failed task that would resume into the old flow.
@pytest.mark.parametrize("crashed", ["spec", "awaiting-spec-review"])
def test_failed_spec_task_stays_failed_and_a_resume_starts_a_fresh_spec_session(
        tmp_path, monkeypatch, capsys, crashed):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 400, "failed", crashed_stage=crashed, spec_path=DESIGN,
             spec_retries=1, worktree=str(tmp_path / "gone"))

    status, text = migrate(c, capsys)

    assert status == 0, text
    t = load(c.state_dir, TARGET, 400)
    assert (t.stage, t.park, t.crashed_stage) == (Stage.FAILED, "", "spec")
    assert (t.asked, t.spec_path, t.spec_retries) == (True, "", 0)
    assert next_pass(c).spawned == []                    # not revived by the migration
    wt = Path(c.targets[0].worktrees_path) / "task-400"      # old_task made it
    save(c.state_dir, replace(load(c.state_dir, TARGET, 400), worktree=str(wt)))
    intents.write_intent(c.state_dir, "resume", TARGET, 400, {}, "op", 1)
    sess = next_pass(c)
    assert [s[:2] for s in sess.spawned] == [(400, "spec")]
    assert sess.resumed == []


@pytest.mark.parametrize("stage, extra", [
    ("failed", {"crashed_stage": "implement"}), ("failed", {}), ("done", {}),
    ("canceled", {}), ("queued", {}), ("blocked", {}), ("stalled-on-budget", {}),
    ("address-review", {"pr_number": 5}), ("pr-open", {"pr_number": 5})])
def test_every_other_stage_is_untouched(tmp_path, monkeypatch, capsys, stage, extra):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 500, stage, **extra)
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status == 0, text
    assert snapshot(c.state_dir) == before
    assert any(ln.startswith("untouched") and "#500" in ln for ln in text.splitlines())


# A second run, and a run long after the deploy.
def test_tasks_of_the_new_flow_are_untouched_and_never_must_drain(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    for issue, stage in ((1, Stage.SPEC), (2, Stage.PLAN), (3, Stage.IMPLEMENT),
                         (4, Stage.REVIEW), (5, Stage.AWAITING_PLAN_REVIEW)):
        make_task(c, issue=issue, stage=stage)
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status == 0, text
    assert snapshot(c.state_dir) == before
    assert "0 converted, 5 untouched, 0 must-drain" in text.splitlines()[-1]


def test_summary_line_counts_each_outcome(tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    pr_open(c)

    status, text = migrate(c, capsys)

    assert status == 0
    assert "2 converted, 1 untouched, 0 must-drain" in text.splitlines()[-1]


# The old design path.
def test_old_design_path_never_reaches_the_publish_backstop(tmp_path, monkeypatch,
                                                            capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    published = []
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: published.append(kw["artifact"]))

    migrate(c, capsys)
    next_pass(c)

    assert load(c.state_dir, TARGET, 384).spec_path == ""
    assert published == []


# The old session.
def _stale_done(wt):
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": DESIGN, "track": "standard"}))


def test_live_old_session_is_ended_and_its_stale_done_does_not_advance_the_task(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    wt = gated_spec(c)
    _stale_done(wt)

    migrate(c, capsys)
    sess = FakeSessions(alive={384})
    main.run_pass(c, deps(sess=sess))

    assert sess.ended == [384]
    assert [s[:2] for s in sess.spawned] == [(384, "spec")]
    assert sess.resumed == [] and sess.sent_text == []
    assert load(c.state_dir, TARGET, 384).stage is Stage.SPEC
    assert read_stage_signal(wt).status == "working"


def test_stale_done_is_not_read_while_the_restart_waits_for_capacity(
        tmp_path, monkeypatch, capsys):
    c = replace(cfg_(tmp_path, monkeypatch), capacity=0)
    wt = old_task(c, 370, "spec", spec_path=DESIGN)      # unparked, session live
    _stale_done(wt)

    migrate(c, capsys)
    sess = FakeSessions(alive={370})
    main.run_pass(c, deps(sess=sess))

    assert sess.spawned == [] and sess.resumed == [] and sess.sent_text == []
    t = load(c.state_dir, TARGET, 370)
    assert (t.stage, t.park) == (Stage.SPEC, PARK_WAKE)


# Writes.
def test_a_failed_write_leaves_no_temp_file_and_the_old_file(tmp_path, monkeypatch,
                                                            capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    before = task_file(c, 384).read_bytes()

    def no_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", no_replace)

    with pytest.raises(OSError):
        openspec_migration.main([c.state_dir])

    monkeypatch.undo()
    assert task_file(c, 384).read_bytes() == before
    assert [p.name for p in Path(c.state_dir).iterdir()
            if p.name != "convergence.lock"] == [task_file(c, 384).name]


def test_usage_error_without_a_state_dir(capsys):
    assert openspec_migration.main([]) != 0
    assert "usage" in capsys.readouterr().out.lower()


def test_pass_lock_non_blocking_raises_while_held(tmp_path):
    with pass_lock(str(tmp_path)):
        with pytest.raises(BlockingIOError):
            with pass_lock(str(tmp_path), blocking=False):
                pass
    with pass_lock(str(tmp_path), blocking=False):       # free again
        pass
