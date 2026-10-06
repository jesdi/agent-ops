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
from dispatcher.state import (PARK_REVIEW, PARK_WAKE, PlanApprovalRequest, Stage,
                              load, read_stage_signal, save)

import tests.test_gate_skip_acceptance as gate
from tests.test_main import FakeSessions, deps, make_task, write_tickets
from tests.test_openspec_migration_acceptance import (
    DESIGN, QUESTIONNAIRE, TARGET, cfg_, gated_spec, next_pass, old_task,
    parked_spec, pr_open, snapshot, task_file)


OLD_GATE = "awaiting-spec-review"


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
                                     '{"issue": 999, "target": "t"}', "\xff"])
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
    assert (d["asked"], d["gated"]) == (True, False)
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


# Nothing the migration writes lets a task pass the plan review gate.
SHAPES = {
    "spec, session live": ("spec", {}),
    "spec, parked for answers": ("spec", {"park": "parked", "operator_request": {
        "kind": "answers", "path": QUESTIONNAIRE}}),
    "spec, parked for input": ("spec", {"park": "parked", "park_note": "blocked"}),
    "spec, parked at login": ("spec", {"park": "parked-login"}),
    "old gate, request armed": (OLD_GATE, {"park": "awaiting-review",
                                           "operator_request": {"kind": "spec-approval"}}),
    "old gate, inside the grace time": (OLD_GATE, {
        "operator_request": {"kind": "spec-approval"}}),
    "old gate, no request": (OLD_GATE, {"park": "awaiting-review"}),
    "failed in spec": ("failed", {"crashed_stage": "spec"}),
    "failed at the old gate": ("failed", {"crashed_stage": OLD_GATE}),
}


def _gate_free_cfg(tmp_path, monkeypatch):
    """`trivial` is a gate-free track here."""
    c = gate._setup(tmp_path, monkeypatch)
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url="u"))
    return c


def _drive_to_the_ready_report(c, issue, wt):
    """The best case for a gate skip: a fresh spec session reports done on
    the gate-free track, and the plan session reports ready with 0 open
    questions and a clean summary. Returns the sessions of the last pass."""
    if load(c.state_dir, TARGET, issue).stage is Stage.FAILED:
        intents.write_intent(c.state_dir, "resume", TARGET, issue, {}, "op", 1)
    sess = next_pass(c)
    assert [s[:2] for s in sess.spawned] == [(issue, "spec")] and sess.resumed == []
    folder = wt / gate.FOLDER
    folder.mkdir(parents=True)
    (folder / "proposal.md").write_text("# Proposal\n\n## Why\n\n" + "w " * 400)
    (folder / "spec.md").write_text("# Label: Spec\n\n## Requirements\n\n" + "x " * 400
                                    + "\n\n## Scenarios\n\n" + "y " * 400)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": gate.SPEC, "track": "trivial"}))
    sess = FakeSessions(alive={issue})
    main.run_pass(c, deps(sess=sess))
    assert [s[:2] for s in sess.spawned] == [(issue, "plan")]
    (folder / "design.md").write_text("# Label: Design\n\n" + "d " * 400)
    write_tickets(wt, 1)
    (wt / gate.SUMMARY).write_text(gate.NONE_SUMMARY)
    gate._ready(wt)
    sess = FakeSessions(alive={issue})
    main.run_pass(c, deps(sess=sess))
    return sess


def test_the_drive_skips_the_gate_for_a_task_that_never_asked(tmp_path, monkeypatch):
    """Control: without the migration's `asked`, this drive reaches implement."""
    c = _gate_free_cfg(tmp_path, monkeypatch)
    wt = make_task(c, issue=370, stage=Stage.SPEC, track="trivial",
                   park=PARK_WAKE, crashed_stage="spec")

    sess = _drive_to_the_ready_report(c, 370, wt)

    assert [s[:2] for s in sess.spawned] == [(370, "implement")]


@pytest.mark.parametrize("shape", SHAPES)
def test_every_converted_task_waits_at_the_plan_review_gate(tmp_path, monkeypatch,
                                                            capsys, shape):
    c = _gate_free_cfg(tmp_path, monkeypatch)
    stage, extra = SHAPES[shape]
    wt = old_task(c, 370, stage, track="trivial", **extra)   # no questionnaire file

    status, text = migrate(c, capsys)

    assert status == 0, text
    d = raw(c, 370)
    assert (d["asked"], d["gated"], d["operator_request"]) == (True, False, None)
    assert d["stage"] in ("spec", "failed") and d["stage"] != "plan"
    sess = _drive_to_the_ready_report(c, 370, wt)
    t = load(c.state_dir, TARGET, 370)
    assert t.stage is Stage.AWAITING_PLAN_REVIEW
    assert [s for s in sess.spawned if s[1] == "implement"] == []


# A value the migration does not know is reported, never coerced.
@pytest.mark.parametrize("stage, extra, named", [
    ("spec-approved", {}, "unknown stage"),
    ("spec", {"park": "approved"}, "unknown park"),
    ("spec", {"operator_request": {"kind": "approval"}}, "unknown request kind"),
    ("spec", {"operator_request": {"path": "x"}}, "unknown request kind"),
    ("spec", {"operator_request": "spec-approval-ish"}, "unknown request kind"),
    ("pr-open", {"operator_request": {"kind": "spec-approval"}}, "spec-approval request"),
    ("spec", {"target": None}, "no issue or target"),
])
def test_an_unknown_value_refuses_the_whole_run(tmp_path, monkeypatch, capsys,
                                                stage, extra, named):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    old_task(c, 370, stage, **extra)
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status != 0
    assert snapshot(c.state_dir) == before
    assert any(ln.startswith("unreadable") and task_file(c, 370).name in ln
               and named in ln for ln in text.splitlines()), text


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
    assert (t.asked, t.gated, t.spec_path, t.spec_retries) == (True, False, "", 0)
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
                         (4, Stage.REVIEW)):
        make_task(c, issue=issue, stage=stage, asked=True)
    # A task that waits for the operator's approval keeps exactly that.
    make_task(c, issue=5, stage=Stage.AWAITING_PLAN_REVIEW, gated=True,
              park=PARK_REVIEW, operator_request=PlanApprovalRequest(gate.SUMMARY))
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status == 0, text
    assert snapshot(c.state_dir) == before
    assert "0 converted, 5 untouched, 0 must-drain" in text.splitlines()[-1]
    t = load(c.state_dir, TARGET, 5)
    assert (t.gated, t.operator_request.kind) == (True, "plan-approval")
    assert load(c.state_dir, TARGET, 1).asked is True


def test_second_run_keeps_the_first_run_and_the_new_flow_byte_for_byte(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    old_task(c, 400, "failed", crashed_stage="spec")
    make_task(c, issue=5, stage=Stage.AWAITING_PLAN_REVIEW, gated=True,
              park=PARK_REVIEW, operator_request=PlanApprovalRequest(gate.SUMMARY))
    make_task(c, issue=6, stage=Stage.SPEC, asked=True)
    assert migrate(c, capsys)[0] == 0
    after_first = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status == 0, text
    assert snapshot(c.state_dir) == after_first
    assert "0 converted, 5 untouched" in text.splitlines()[-1]


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
