"""The decisions of the old-flow migration that the locked acceptance file
(tests/test_openspec_migration_acceptance.py) leaves open.

Deleted together with dispatcher/openspec_migration.py after the deploy. Every
state directory is a pytest tmp_path; the command runs in-process.
"""
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import intents, messages, openspec_migration, spec_publish
from dispatcher.convergence import pass_lock
from dispatcher.state import (PARK_REVIEW, PARK_WAKE, PlanApprovalRequest, Stage,
                              load, read_stage_signal, resumable_crash, save)

import tests.test_gate_skip_acceptance as gate
from tests.test_main import FakeSessions, deps, make_task, write_tickets
from tests.test_openspec_migration_acceptance import (
    DESIGN, QUESTIONNAIRE, TARGET, cfg_, gated_spec, next_pass, old_task,
    parked_spec, pr_open, snapshot, task_file)


OLD_GATE = "awaiting-spec-review"


def migrate(c, capsys, *flags):
    """(exit status, output) of one run."""
    capsys.readouterr()
    status = openspec_migration.main([*flags, c.state_dir])
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
                                     '{"issue": 999, "target": "t"}', "\xff",
                                     '{"issue": 999, "stage": "spec"}'])
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
    written = raw(c, 384)["updated_at"]

    status, text = migrate(c, capsys)

    assert status == 0, text
    d = raw(c, 384)
    assert (d["stage"], d["park"], d["crashed_stage"]) == ("spec", PARK_WAKE, "spec")
    assert d["operator_request"] is None
    assert d["spec_path"] == ""
    assert (d["asked"], d["gated"]) == (True, False)
    assert "ticket_cursor" not in d
    assert d["updated_at"] == written                      # keeps its place in the queue
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
@pytest.mark.parametrize("stage, extra, field, value", [
    ("spec-approved", {}, "stage", "'spec-approved'"),
    ("spec", {"park": "approved"}, "park", "'approved'"),
    ("spec", {"operator_request": {"kind": "approval"}}, "operator_request.kind",
     "'approval'"),
    ("spec", {"operator_request": {"path": "x"}}, "operator_request.kind", "''"),
    ("spec", {"operator_request": "spec-approval-ish"}, "operator_request.kind",
     "'spec-approval-ish'"),
    ("pr-open", {"operator_request": {"kind": "spec-approval"}},
     "operator_request.kind", "'spec-approval'"),
])
def test_an_unknown_value_refuses_the_whole_run(tmp_path, monkeypatch, capsys,
                                                stage, extra, field, value):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    old_task(c, 370, stage, **extra)
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status != 0
    assert snapshot(c.state_dir) == before
    assert any(ln.startswith("unknown-value") and task_file(c, 370).name in ln
               and f"field {field} " in ln and value in ln
               for ln in text.splitlines()), text
    # The file is a valid task: the advice must never be to remove it.
    assert "do not delete" in text and "remove" not in text


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
    ("failed", {"crashed_stage": "address-review"}), ("failed", {}), ("done", {}),
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
def test_a_failed_write_stops_the_run_and_a_second_run_finishes_it(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    for issue in (381, 382, 383):
        gated_spec(c, issue)
    real, calls = os.replace, []

    def second_fails(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        real(src, dst)
    monkeypatch.setattr(os, "replace", second_fails)

    status, text = migrate(c, capsys)

    monkeypatch.undo()
    assert status != 0
    stopped = f"stopped at {task_file(c, 382).name}: disk full; run again"
    assert stopped in text.splitlines(), text
    assert "Traceback" not in text
    assert [raw(c, i)["stage"] for i in (381, 382, 383)] == [
        "spec", OLD_GATE, OLD_GATE]
    assert sorted(p.name for p in Path(c.state_dir).iterdir()) == sorted(
        ["convergence.lock"] + [task_file(c, i).name for i in (381, 382, 383)])

    status, text = migrate(c, capsys)

    assert status == 0, text
    assert [raw(c, i)["stage"] for i in (381, 382, 383)] == ["spec"] * 3
    assert "2 converted, 1 untouched" in text.splitlines()[-1]


# What the operator told the old session.
def _said(c, issue, text, actor="operator", delivered=True):
    m = messages.append(c.state_dir, TARGET, issue, text, actor)
    if delivered:
        messages.mark_delivered(c.state_dir, TARGET, issue, [m.id])
    return m


def _texts(c, issue, delivered):
    return [m.text for m in messages.all_messages(c.state_dir, TARGET, issue)
            if bool(m.delivered_at) is delivered]


def test_delivered_answers_ride_in_the_fresh_spec_prompt(tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 370, "spec")                             # answered, session resumed
    first = _said(c, 370, "1: yes. 2: option B. 3: no.")
    _said(c, 370, "E2E run 5 concluded: success — fetch logs", actor="dispatcher")
    _said(c, 370, "4: keep. 5: 30 days.", actor="dispatcher")   # a Telegram reply
    _said(c, 370, "", actor="dispatcher")                # /attach queues no text
    _said(c, 370, "6: later", delivered=False)

    assert migrate(c, capsys)[0] == 0
    assert _texts(c, 370, delivered=False) == [
        "1: yes. 2: option B. 3: no.", "4: keep. 5: 30 days.", "6: later"]
    sess = next_pass(c)

    prompt = sess.spawned[0][3]
    block = prompt[prompt.index("## Operator messages"):]
    assert f"- [{first.created_at}] operator: 1: yes. 2: option B. 3: no." in block
    assert (block.index("option B") < block.index("4: keep. 5: 30 days.")
            < block.index("6: later"))
    assert "E2E run 5" not in block
    assert _texts(c, 370, delivered=False) == []         # stamped once


def test_change_request_at_the_old_gate_rides_in_the_fresh_spec_prompt(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    _said(c, 384, "change section 2")

    assert migrate(c, capsys)[0] == 0
    sess = next_pass(c)

    assert "operator: change section 2" in sess.spawned[0][3]


def test_messages_of_an_untouched_task_and_lines_that_are_no_message_stay(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    pr_open(c)
    _said(c, 391, "fix the review comment")
    _said(c, 384, "change section 2")
    gate_file = Path(c.state_dir) / "messages" / f"{TARGET}-384.jsonl"
    gate_file.write_text(gate_file.read_text() + "not json\n")
    pr_file = Path(c.state_dir) / "messages" / f"{TARGET}-391.jsonl"
    before = pr_file.read_bytes()

    assert migrate(c, capsys)[0] == 0

    assert pr_file.read_bytes() == before
    assert gate_file.read_text().splitlines()[-1] == "not json"
    assert _texts(c, 384, delivered=False) == ["change section 2"]


def test_an_unreadable_message_file_refuses_the_whole_run(tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    bad = Path(c.state_dir) / "messages" / f"{TARGET}-384.jsonl"
    bad.parent.mkdir()
    bad.write_bytes(b"\xff\xfe")
    before = snapshot(c.state_dir)

    status, text = migrate(c, capsys)

    assert status != 0
    assert snapshot(c.state_dir) == before
    assert any(ln.startswith("unreadable") and f"messages/{bad.name}" in ln
               for ln in text.splitlines()), text


def test_a_run_that_stops_between_messages_and_task_file_loses_no_message(
        tmp_path, monkeypatch, capsys):
    """The messages are written first: the task file is still old when the
    run stops, so the next run does the task again."""
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    _said(c, 384, "change section 2")
    real, calls = os.replace, []

    def second_fails(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        real(src, dst)
    monkeypatch.setattr(os, "replace", second_fails)
    assert migrate(c, capsys)[0] == 1
    monkeypatch.setattr(os, "replace", real)             # undo() would drop cfg_'s patches

    assert raw(c, 384)["stage"] == OLD_GATE
    assert migrate(c, capsys)[0] == 0
    sess = next_pass(c)

    assert [s[:2] for s in sess.spawned] == [(384, "spec")]
    assert "operator: change section 2" in sess.spawned[0][3]


# An old task that failed in plan, implement or review.
@pytest.mark.parametrize("crashed", ["plan", "implement", "review"])
def test_old_task_failed_past_the_spec_is_labelled_and_made_not_resumable(
        tmp_path, monkeypatch, capsys, crashed):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 412, "failed", crashed_stage=crashed, spec_path=DESIGN, ticket_cursor=2)
    before = raw(c, 412)

    status, text = migrate(c, capsys)

    assert status == 0, text
    assert raw(c, 412) == {
        **before, "asked": True, "crashed_stage": "",
        "park_note": f"failed in {crashed} in the old flow; not resumable"}
    assert load(c.state_dir, TARGET, 412).stage is Stage.FAILED
    assert not resumable_crash(load(c.state_dir, TARGET, 412))
    assert f"do-not-resume  {TARGET} #412  (failed, crashed in {crashed})" in text
    assert "1 do-not-resume" in text.splitlines()[-1]
    after_first = snapshot(c.state_dir)
    assert migrate(c, capsys)[0] == 0
    assert snapshot(c.state_dir) == after_first


def test_a_resume_of_an_old_failed_plan_task_starts_nothing(
        tmp_path, monkeypatch, capsys):
    c = _gate_free_cfg(tmp_path, monkeypatch)
    old_task(c, 412, "failed", crashed_stage="plan", track="trivial",
             spec_path=gate.SPEC, park_note="an older note")

    assert migrate(c, capsys)[0] == 0
    intents.write_intent(c.state_dir, "resume", TARGET, 412, {}, "op", 1)
    sess = next_pass(c)

    assert sess.spawned == [] and sess.resumed == []
    t = load(c.state_dir, TARGET, 412)
    assert (t.stage, t.asked, t.park_note) == (Stage.FAILED, True, "an older note")


def test_old_failed_task_with_no_crashed_stage_is_named_not_resumable(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 411, "failed", spec_path=DESIGN)         # e.g. failed at the old gate

    status, text = migrate(c, capsys)

    assert status == 0
    assert f"untouched  {TARGET} #411  (failed, not resumable)" in text


# --check: the check phase alone.
def test_check_writes_nothing_and_takes_no_lock(tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    _said(c, 384, "change section 2")
    before = snapshot(c.state_dir)

    with pass_lock(c.state_dir):                         # a pass may be running
        status, text = migrate(c, capsys, "--check")
    Path(c.state_dir, "convergence.lock").unlink()

    assert status == 0, text
    assert snapshot(c.state_dir) == before
    assert any(ln.startswith("would-convert") and "#384" in ln for ln in text.splitlines())
    assert not any(ln.startswith("converted") for ln in text.splitlines())
    assert text.splitlines()[-1].startswith("2 would-convert, 0 untouched")
    assert "nothing was changed" in text.splitlines()[-1]


def test_check_has_the_exit_status_of_the_real_run(tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)
    old_task(c, 377, "implement")

    status, text = migrate(c, capsys, "--check")

    assert status == 1
    assert any(ln.startswith("must-drain") and "#377" in ln for ln in text.splitlines())
    assert not Path(c.state_dir, "convergence.lock").exists()
    assert openspec_migration.main(["--check", str(tmp_path / "nope")]) == 2
    assert openspec_migration.main(["--chek", c.state_dir]) == 2


# The guard: no dispatcher pass before the migration.
def _run_main(c, monkeypatch, capsys, *flags):
    """dispatcher.main.main() as the unit runs it; (exit status, stderr, passes)."""
    passes = []
    monkeypatch.setattr(main, "load_config", lambda path: c)
    monkeypatch.setattr(main, "guarded_pass", lambda *a, **kw: passes.append(a))
    monkeypatch.setattr(main.tmux_migration, "migrate",
                        lambda *a: passes.append(a) or [])
    monkeypatch.setattr(sys, "argv", ["agent-ops-dispatcher", "--config", "t.yaml", *flags])
    capsys.readouterr()
    try:
        main.main()
        status = 0
    except SystemExit as exc:
        status = exc.code
    return status, capsys.readouterr().err, passes


@pytest.mark.parametrize("flags", [(), ("--migrate-tmux",)])
@pytest.mark.parametrize("stage, extra", [
    (OLD_GATE, {"park": "awaiting-review", "operator_request": {"kind": "spec-approval"}}),
    ("spec", {"park": "parked"}), ("implement", {}),
    ("failed", {"crashed_stage": "plan"}), (OLD_GATE, {"park": "no-such-park"})])
def test_a_pass_is_refused_while_an_old_flow_task_is_on_disk(
        tmp_path, monkeypatch, capsys, flags, stage, extra):
    c = cfg_(tmp_path, monkeypatch)
    old_task(c, 384, stage, **extra)
    intents.write_intent(c.state_dir, "reply", TARGET, 384, {"text": "approved"}, "op", 1)
    before = snapshot(c.state_dir)

    status, err, passes = _run_main(c, monkeypatch, capsys, *flags)

    assert status == 1 and passes == []
    assert snapshot(c.state_dir) == before               # the intent is still there
    assert not Path(c.state_dir, "convergence.lock").exists()
    assert f"python -m dispatcher.openspec_migration {c.state_dir}" in err
    assert task_file(c, 384).name in err


def test_the_pass_runs_after_the_migration(tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    parked_spec(c)
    gated_spec(c)
    pr_open(c)
    old_task(c, 412, "failed", crashed_stage="plan")
    assert _run_main(c, monkeypatch, capsys)[0] == 1
    assert migrate(c, capsys)[0] == 0

    status, err, passes = _run_main(c, monkeypatch, capsys)

    assert status == 0 and len(passes) == 1 and err == ""


def test_a_new_flow_box_and_a_corrupt_file_do_not_stop_the_pass(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    for issue, stage in ((1, Stage.SPEC), (2, Stage.PLAN), (3, Stage.IMPLEMENT),
                         (4, Stage.FAILED), (5, Stage.PR_OPEN)):
        make_task(c, issue=issue, stage=stage)
    old_task(c, 6, "pr-open", pr_number=7)               # old file, nothing to migrate
    task_file(c, 999).write_text('{"issue": 999, "stage": "sp')   # the loader skips it
    task_file(c, 998).write_text("[1]")

    status, err, passes = _run_main(c, monkeypatch, capsys)

    assert status == 0 and len(passes) == 1


def test_the_guard_does_not_stop_the_commands_that_write_no_task(
        tmp_path, monkeypatch, capsys):
    c = cfg_(tmp_path, monkeypatch)
    gated_spec(c)

    status, _, _ = _run_main(c, monkeypatch, capsys, "--triage")

    assert status == 0


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
