"""dispatcher/lifecycle.py: the task lifecycle table (#132, ADR 0014)."""
import builtins
import itertools
import os
import socket
import time
from unittest import mock

import pytest

from dispatcher import lifecycle
from dispatcher.lifecycle import (TABLE, AcquireSlot, At, CancelIssue, Cause,
                                  FailReason, HumanReason,
                                  ContinueSession, EndSession, Event, EventType,
                                  FinishMerged, Ignore, Kind, Reject,
                                  ReleaseCard, ReleaseSlot, ResetRounds,
                                  RespawnStage, SetResumeOverride,
                                  SpawnAddressReview, Status, SweepTombstone,
                                  Transition, TypeLoginCode, WakeBlocked,
                                  decide, graph, legacy_view, next_transitions,
                                  occupies, operator_request)
from dispatcher.lifecycle import (Attempt, Death, Gate, LaunchFailure, LoginCode, Notify, Pane,
                                  Ranked, Report, ResumeChoice, Silence, Stranded, Wait,
                                  Woken)
from dispatcher.state import (AnswersRequest, PlanApprovalRequest, Stage, StageSignal,
                              TaskState)

ANSWERS = AnswersRequest(path="q.html", fingerprint="ab12")
PLAN = PlanApprovalRequest(path="review.html", fingerprint="cd34")


def task(stage=Stage.IMPLEMENT, **kw) -> TaskState:
    return TaskState(issue=363, repository="portfolio_eval", stage=stage, slot=0,
                     worktree="/wt", branch="agent/task-363", title="t",
                     updated_at="2026-11-13T00:00:00+00:00", **kw)


def human(reason, note=""):
    return Status(Kind.PARKED_HUMAN, reason=HumanReason(reason), note=note)


def failed(reason):
    return Status(Kind.FAILED, reason=FailReason(reason))


def waking(origin, cause):
    return Status(Kind.WAKING, origin=origin, cause=Cause(cause))


S = Stage
REVIEW_PARK = Status(Kind.PARKED_REVIEW, path="review.html", fingerprint="cd34")
ANSWERS_PARK = Status(Kind.PARKED_ANSWERS, path="q.html", fingerprint="ab12")

# The table of the scenario "migration command rewrites each legacy shape".
MIGRATION = [
    (dict(stage=S.IMPLEMENT), S.IMPLEMENT, Status(Kind.RUNNING)),
    (dict(park="parked", park_reason="decision", park_note="which DB?"),
     S.IMPLEMENT, human("decision", "which DB?")),
    (dict(park="parked"), S.IMPLEMENT, human("operator")),
    (dict(stage=S.QUEUED, park="parked", park_reason="access"),
     S.QUEUED, Status(Kind.PARKED_ACCESS)),
    (dict(stage=S.SPEC, park="parked", operator_request=ANSWERS), S.SPEC, ANSWERS_PARK),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="awaiting-review", operator_request=PLAN),
     S.PLAN, REVIEW_PARK),
    (dict(stage=S.AWAITING_PLAN_REVIEW, operator_request=PLAN), S.PLAN,
     Status(Kind.HELD_PLAN_APPROVAL, path="review.html", fingerprint="cd34")),
    (dict(park="awaiting-ci", ci_run_id=991), S.IMPLEMENT, Status(Kind.PARKED_CI, run_id=991)),
    (dict(stage=S.SPEC, park="parked-login"), S.SPEC, Status(Kind.HELD_LOGIN)),
    (dict(stage=S.PLAN, park="unpark-requested"), S.PLAN,
     waking(human("operator"), "reply")),
    (dict(park="unpark-requested", crashed_stage="implement"), S.IMPLEMENT,
     waking(failed("session-crash"), "resume")),
    (dict(stage=S.SPEC, park="unpark-requested", operator_request=ANSWERS), S.SPEC,
     waking(ANSWERS_PARK, "reply")),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="unpark-requested", operator_request=PLAN),
     S.PLAN, waking(REVIEW_PARK, "reply")),
    (dict(stage=S.QUEUED, park="unpark-requested", park_reason="access"), S.QUEUED,
     waking(Status(Kind.PARKED_ACCESS), "access")),
    (dict(stage=S.QUEUED, unlaunched=True), S.QUEUED, waking(None, "launch")),
    (dict(implement_providers=["anthropic"]), S.REVIEW, waking(None, "next-stage")),
    (dict(stage=S.PR_OPEN, feedback_pending=True, attention="conflict"), S.PR_OPEN,
     waking(Status(Kind.PARKED_PR), "attention(conflict)")),
    (dict(stage=S.PR_OPEN), S.PR_OPEN, Status(Kind.PARKED_PR)),
    (dict(stage=S.PR_OPEN, park="parked", park_reason="loop cap"), S.PR_OPEN,
     human("loop-cap")),
    (dict(stage=S.FAILED, crashed_stage="implement"), S.IMPLEMENT, failed("session-crash")),
    (dict(stage=S.FAILED, park="parked", pr_number=88), S.PR_OPEN, failed("unknown")),
    (dict(stage=S.FAILED, pr_number=88), S.PR_OPEN, failed("unknown")),
    (dict(stage=S.FAILED), S.QUEUED, failed("unknown")),
    (dict(stage=S.DONE, pr_number=88), S.PR_OPEN, Status(Kind.DONE)),
    (dict(stage=S.CANCELED), S.QUEUED, Status(Kind.CANCELED)),
    (dict(stage=S.BLOCKED), S.IMPLEMENT,
     human("operator", "migrated from legacy blocked stage")),
    (dict(stage=S.STALLED_ON_BUDGET), S.SPEC,
     human("operator", "migrated from legacy stalled-on-budget stage")),
]


@pytest.mark.parametrize("shape, stage, status", MIGRATION)
def test_the_view_reads_each_legacy_shape_as_the_migration_table_names(shape, stage, status):
    assert legacy_view(task(**shape)) == (stage, status)


def test_a_task_exposes_its_status_view_with_the_stage_it_belongs_to():
    t = task(park="parked", park_reason="decision", park_note="which DB?")
    assert t.at == At(S.IMPLEMENT, human("decision", "which DB?"))
    done = task(implement_providers=["anthropic"])
    assert done.at == At(S.REVIEW, waking(None, "next-stage"))
    assert not hasattr(done, "status")  # never paired with the raw stage


@pytest.mark.parametrize("shape", [
    dict(park="awaiting-ci", ci_run_id=0),
    dict(park="parked", park_reason="no such reason"),
    dict(stage=S.AWAITING_PLAN_REVIEW, operator_request=ANSWERS),
])
def test_a_shape_the_table_does_not_classify_is_refused(shape):
    with pytest.raises(ValueError):
        legacy_view(task(**shape))



@pytest.mark.parametrize("kind", list(Kind))
def test_occupies_exactly_running_and_held(kind):
    assert occupies(Status(kind)) is (kind in {
        Kind.RUNNING, Kind.HELD_PLAN_APPROVAL, Kind.HELD_LOGIN})


@pytest.mark.parametrize("shape, asked", [
    (dict(stage=S.AWAITING_PLAN_REVIEW, operator_request=PLAN), PLAN),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="awaiting-review", operator_request=PLAN), PLAN),
    (dict(stage=S.SPEC, park="parked", operator_request=ANSWERS), ANSWERS),
    (dict(stage=S.SPEC, park="unpark-requested", operator_request=ANSWERS), ANSWERS),
    (dict(stage=S.AWAITING_PLAN_REVIEW, park="unpark-requested", operator_request=PLAN),
     PLAN),
    (dict(park="unpark-requested", crashed_stage="implement"), None),
    (dict(stage=S.QUEUED, unlaunched=True), None),
    (dict(park="parked", park_reason="decision"), None),
    (dict(), None),
    (dict(stage=S.DONE, pr_number=88), None),
])
def test_operator_request_is_derived_from_the_status(shape, asked):
    got = operator_request(task(**shape))
    assert (got, getattr(got, "fingerprint", None)) == (
        asked, getattr(asked, "fingerprint", None))


E = EventType
RUNNING = Status(Kind.RUNNING)
GATE = Status(Kind.HELD_PLAN_APPROVAL, path="review.html", fingerprint="cd34")
LOOP_CAP = human("loop-cap")
PR = Status(Kind.PARKED_PR)


# One task per status kind, plus the stages and details the guards read.
TASKS = [
    At(S.IMPLEMENT, RUNNING), At(S.PLAN, GATE),
    At(S.SPEC, Status(Kind.HELD_LOGIN)), At(S.IMPLEMENT, human("decision", "which DB?")),
    At(S.SPEC, ANSWERS_PARK), At(S.QUEUED, Status(Kind.PARKED_ACCESS)),
    At(S.IMPLEMENT, Status(Kind.PARKED_CI, run_id=991)), At(S.PLAN, REVIEW_PARK),
    At(S.PR_OPEN, PR), At(S.PR_OPEN, LOOP_CAP),
    At(S.IMPLEMENT, waking(human("decision"), "reply")),
    At(S.PR_OPEN, waking(PR, "attention(feedback)")),
    At(S.QUEUED, waking(None, "launch")), At(S.REVIEW, waking(None, "next-stage")),
    At(S.IMPLEMENT, failed("session-crash")), At(S.PR_OPEN, failed("pr-closed")),
    At(S.QUEUED, failed("unknown")), At(S.PLAN, Status(Kind.CANCELED)),
    At(S.PR_OPEN, Status(Kind.DONE)),
]
PLAIN_GATE = Gate(at=False, gated=False, rounds_used=False, respawn_pending=False,
                  armed=False)


def report(signal: StageSignal, **facts) -> Report:
    """A session-signal's facts: those named, the rest plain."""
    plain = dict(round=None, gate=PLAIN_GATE, retries=frozenset(), asks=False, bad_track="",
                 skips_gate=False, tickets=None, revision=None, spec=None, answers=None,
                 launch_denied=False, login="")
    return Report(signal, **{**plain, **facts})


# The facts each event type is tried with.
DETAILS = {
    E.SESSION_SIGNAL: [report(StageSignal("implement", s)) for s in ("working", "blocked")],
    E.SESSION_IDLE: [Silence(round=None, note="", pane=Pane.CLEAR, login=""),
                     Silence(round=None, note="stalled", pane=None, login="")],
    E.SESSION_WAITING: [Wait(round=None, note="", record=None, login="")],
    E.SESSION_DIED: [Death(at_gate=False, rounds_used=False, launch_denied=False)],
    E.TRACK_UNCONFIGURED: [Stranded(note="gone", login="")],
    E.LAUNCH_FAILED: [LaunchFailure(S.SPEC)],
    E.RESUME_ADMITTED: [Woken(""), Woken("capacity full")],
    E.CANDIDATE_RANKED: [Ranked(False), Ranked(True)],
    E.OP_RESUME: [ResumeChoice("", False), ResumeChoice("anthropic/claude-opus-5", True)],
    E.OP_LOGIN_CODE: [LoginCode("ABCD-1234", False), LoginCode("ABCD-1234", True)],
    **{e: [Attempt(False), Attempt(True)]
       for e in (E.CI_FINISHED, E.PR_CHECK_FAILED, E.PR_CONFLICT)},
}


def events():
    """Every event type with each set of facts it is tried with."""
    for e in EventType:
        for detail in DETAILS.get(e, [None]):
            yield Event(e, detail)


EVENTS = list(events())


def undeclared(event_types) -> list[tuple[str, str]]:
    """Every (status kind, event type) pair with no row that decides it
    whatever the facts: unguarded and at any stage."""
    declared = {(r.source, r.event) for r in TABLE
                if r.guard is lifecycle.always and r.move is None}
    return [(k.value, getattr(e, "value", e)) for e in event_types for k in Kind
            if (k, e) not in declared]


def test_the_table_is_total():
    assert undeclared(EventType) == []


def test_an_event_type_with_no_rows_names_every_status_kind_missing_one():
    missing = undeclared([*EventType, "liveness-unknown"])
    assert missing == [(k.value, "liveness-unknown") for k in Kind]


def test_decide_answers_every_pair_without_io():
    refuse = mock.Mock(side_effect=AssertionError("decide did I/O"))
    with mock.patch.multiple(builtins, open=refuse), \
            mock.patch.multiple(os, stat=refuse, listdir=refuse, scandir=refuse), \
            mock.patch.multiple(time, time=refuse, monotonic=refuse), \
            mock.patch.multiple(socket, socket=refuse):
        answers = [decide(at, event) for at, event in itertools.product(TASKS, EVENTS)]
    assert all(isinstance(a, (Transition, Ignore, Reject)) for a in answers)
    refuse.assert_not_called()


def test_a_transition_into_failed_canceled_or_done_keeps_the_stage():
    """Requirement 1's one exception: a launch that raises fails the task
    at the stage that could not start."""
    ends = {Kind.FAILED, Kind.CANCELED, Kind.DONE}
    for at, event in itertools.product(TASKS, EVENTS):
        if event.type is E.LAUNCH_FAILED:
            continue
        got = decide(at, event)
        if isinstance(got, Transition) and got.status.kind in ends:
            assert got.stage is at.stage, (at, event)


def test_a_transition_takes_a_slot_exactly_when_it_starts_occupying():
    for at, event in itertools.product(TASKS, EVENTS):
        got = decide(at, event)
        if not isinstance(got, Transition):
            continue
        gains = occupies(got.status) and not occupies(at.status)
        loses = occupies(at.status) and not occupies(got.status)
        assert (AcquireSlot() in got.actions, ReleaseSlot() in got.actions) == (
            gains, loses), (at, event)


def test_decide_reads_a_task_through_its_legacy_view():
    t = task(park="parked", park_reason="decision")
    assert decide(t, Event(E.OP_REPLY)) == decide(legacy_view(t), Event(E.OP_REPLY))


@pytest.mark.parametrize("at, event, reason", [
    (At(S.PR_OPEN, failed("pr-closed")), E.OP_RESUME,
     "reopen the PR on GitHub or re-queue the issue"),
    (At(S.QUEUED, failed("unknown")), E.OP_RESUME, "re-queue the issue instead"),
])
def test_resume_of_an_unresumable_failure_is_refused(at, event, reason):
    assert decide(at, Event(event, DETAILS.get(event, [None])[0])) == Reject(reason)


def test_a_done_task_ranked_again_is_refused():
    assert isinstance(decide(At(S.PR_OPEN, Status(Kind.DONE)), Event(E.CANDIDATE_RANKED, Ranked(False))),
                      Reject)


@pytest.mark.parametrize("reason, stage", [("session-crash", S.IMPLEMENT),
                                           ("killed", S.PLAN), ("unknown", S.PR_OPEN)])
def test_resume_of_a_failure_wakes_it(reason, stage):
    got = decide(At(stage, failed(reason)), Event(E.OP_RESUME, ResumeChoice("m", False)))
    assert got == Transition(waking(failed(reason), "resume"), stage,
                             (ResetRounds(), SetResumeOverride("m", False)))


def test_resume_admitted_respawns_a_failed_stage_with_a_slot():
    got = decide(At(S.IMPLEMENT, waking(failed("session-crash"), "resume")),
                 Event(E.RESUME_ADMITTED, Woken("")))
    assert got == Transition(RUNNING, S.IMPLEMENT,
                             (AcquireSlot(), RespawnStage(S.IMPLEMENT)))


def test_resume_on_a_waking_task_replaces_its_overrides_and_stays():
    at = At(S.IMPLEMENT, waking(human("decision"), "reply"))
    got = decide(at, Event(E.OP_RESUME, ResumeChoice("anthropic/claude-opus-5", False)))
    assert got == Transition(at.status, S.IMPLEMENT,
                             (SetResumeOverride("anthropic/claude-opus-5", False),))


def test_park_intent_racing_a_pr_open_is_refused():
    assert isinstance(decide(At(S.PR_OPEN, PR), Event(E.OP_PARK)), Reject)
    assert isinstance(decide(At(S.PLAN, human("operator")), Event(E.OP_PARK)), Reject)


def test_park_intent_parks_a_running_task_for_the_operator():
    got = decide(At(S.REVIEW, RUNNING), Event(E.OP_PARK))
    assert got == Transition(human("operator", "parked by operator"), S.REVIEW,
                             (EndSession(), ReleaseSlot()))


def test_a_blocked_wake_stays_waking():
    at = At(S.IMPLEMENT, waking(human("decision"), "reply"))
    got = decide(at, Event(E.RESUME_ADMITTED, Woken("capacity full")))
    assert got == Transition(at.status, S.IMPLEMENT, (WakeBlocked("capacity full"),))


def test_an_admitted_reply_wake_continues_the_session():
    got = decide(At(S.IMPLEMENT, waking(human("decision"), "reply")),
                 Event(E.RESUME_ADMITTED, Woken("")))
    assert got == Transition(RUNNING, S.IMPLEMENT, (AcquireSlot(), ContinueSession()))


def test_a_claim_waiting_on_the_app_spawns_spec_once_admitted():
    got = decide(At(S.QUEUED, waking(None, "launch")), Event(E.RESUME_ADMITTED, Woken("")))
    assert got == Transition(RUNNING, S.SPEC, (AcquireSlot(), RespawnStage(S.SPEC)))


def test_implement_done_launches_review_once_admitted():
    got = decide(At(S.REVIEW, waking(None, "next-stage")), Event(E.RESUME_ADMITTED, Woken("")))
    assert got == Transition(RUNNING, S.REVIEW, (AcquireSlot(), RespawnStage(S.REVIEW),
                                                 Notify("review_started")))


def test_reply_at_the_gate_ends_the_session_and_wakes_through_the_review_park():
    got = decide(At(S.PLAN, GATE), Event(E.OP_REPLY))
    assert got == Transition(waking(REVIEW_PARK, "reply"), S.PLAN,
                             (EndSession(), ResetRounds(), ReleaseSlot()))
    assert operator_request(At(S.PLAN, got.status)) == PLAN


def test_answers_with_no_open_request_are_refused():
    assert decide(At(S.IMPLEMENT, RUNNING), Event(E.OP_ANSWERS)) == Reject("no open request")
    assert decide(At(S.SPEC, waking(ANSWERS_PARK, "reply")),
                  Event(E.OP_ANSWERS)) == Reject("session busy")


def test_answers_wake_a_task_parked_for_them():
    got = decide(At(S.SPEC, ANSWERS_PARK), Event(E.OP_ANSWERS))
    assert got == Transition(waking(ANSWERS_PARK, "reply"), S.SPEC, (ResetRounds(),))


def test_pr_feedback_wakes_an_address_review_round():
    got = decide(At(S.PR_OPEN, PR), Event(E.PR_FEEDBACK))
    assert got.status == waking(PR, "attention(feedback)")
    got = decide(At(S.PR_OPEN, got.status), Event(E.RESUME_ADMITTED, Woken("")))
    assert got == Transition(RUNNING, S.ADDRESS_REVIEW, (AcquireSlot(), SpawnAddressReview()))


def test_an_exhausted_ci_loop_parks_for_the_operator_where_only_merge_and_close_act():
    got = decide(At(S.PR_OPEN, PR), Event(E.PR_CHECK_FAILED, Attempt(True)))
    assert got.status.kind is Kind.PARKED_HUMAN and got.status.reason is HumanReason.LOOP_CAP
    at = At(S.PR_OPEN, got.status)
    assert isinstance(decide(at, Event(E.PR_CONFLICT, Attempt(False))), Ignore)
    assert decide(at, Event(E.PR_MERGED)).status == Status(Kind.DONE)


def test_reply_to_an_exhausted_pr_task_resets_the_rounds_and_wakes_address_review():
    got = decide(At(S.PR_OPEN, LOOP_CAP), Event(E.OP_REPLY))
    assert got == Transition(waking(LOOP_CAP, "reply"), S.PR_OPEN, (ResetRounds(),))
    got = decide(At(S.PR_OPEN, got.status), Event(E.RESUME_ADMITTED, Woken("")))
    assert SpawnAddressReview() in got.actions and got.stage is S.ADDRESS_REVIEW


def test_a_closed_pr_fails_the_task_and_a_later_reply_is_refused():
    got = decide(At(S.PR_OPEN, LOOP_CAP), Event(E.PR_CLOSED))
    assert got.status == failed("pr-closed") and got.stage is S.PR_OPEN
    assert EndSession() in got.actions
    assert isinstance(decide(At(S.PR_OPEN, got.status), Event(E.OP_REPLY)), Reject)


def test_a_merged_pr_finishes_the_task():
    got = decide(At(S.PR_OPEN, PR), Event(E.PR_MERGED))
    assert got.status == Status(Kind.DONE) and FinishMerged() in got.actions


def test_ci_and_access_wake_their_parks():
    assert decide(At(S.IMPLEMENT, Status(Kind.PARKED_CI, run_id=991)),
                  Event(E.CI_FINISHED, Attempt(False))).status.cause is Cause.CI
    assert decide(At(S.QUEUED, Status(Kind.PARKED_ACCESS)), Event(E.ACCESS_REACHED)
                  ).status == waking(Status(Kind.PARKED_ACCESS), "access")


def test_a_failed_task_ranked_again_wakes_in_place():
    got = decide(At(S.REVIEW, failed("task-crash")), Event(E.CANDIDATE_RANKED, Ranked(False)))
    assert got == Transition(waking(failed("task-crash"), "ranked"), S.REVIEW, (ResetRounds(),))


@pytest.mark.parametrize("at", [At(S.PLAN, Status(Kind.CANCELED)),
                                At(S.PR_OPEN, failed("pr-closed"))])
def test_a_tombstone_ranked_again_is_swept_unless_written_this_pass(at):
    assert decide(at, Event(E.CANDIDATE_RANKED, Ranked(False))) == Transition(
        at.status, at.stage, (SweepTombstone(),))
    assert isinstance(decide(at, Event(E.CANDIDATE_RANKED, Ranked(True))), Ignore)


def test_kill_and_cancel_end_the_task_at_its_stage():
    assert decide(At(S.IMPLEMENT, RUNNING), Event(E.OP_KILL)) == Transition(
        failed("killed"), S.IMPLEMENT, (EndSession(), ReleaseCard(), ReleaseSlot()))
    assert decide(At(S.SPEC, human("decision")), Event(E.OP_CANCEL)) == Transition(
        Status(Kind.CANCELED), S.SPEC, (EndSession(), CancelIssue()))


def test_a_login_code_resumes_a_held_session_still_at_its_prompt():
    at = At(S.SPEC, Status(Kind.HELD_LOGIN))
    assert decide(at, Event(E.OP_LOGIN_CODE, LoginCode("ABCD-1234", False))) == Transition(
        RUNNING, S.SPEC, (TypeLoginCode("ABCD-1234"),))
    assert decide(at, Event(E.OP_LOGIN_CODE, LoginCode("", True))) == Reject(
        "no longer at a login prompt — code not typed")
    assert isinstance(decide(At(S.IMPLEMENT, RUNNING), Event(E.OP_LOGIN_CODE, LoginCode("", False))), Reject)


def test_next_transitions_of_a_task_parked_on_a_decision():
    at = At(S.IMPLEMENT, human("decision"))
    moves = {(e.event, e.to) for e in next_transitions(at)}
    assert {("op-reply", "waking"), ("op-resume", "waking"), ("op-kill", "failed"),
            ("op-cancel", "canceled")} <= moves
    assert "op-park" not in {event for event, _ in moves}
    assert set(next_transitions(at)) <= set(graph()["status"]["edges"])


def test_next_transitions_reads_a_task_through_its_legacy_view():
    t = task(park="parked", park_reason="decision")
    assert next_transitions(t) == next_transitions(legacy_view(t))


def test_graph_draws_every_transition_row_and_the_stage_progression():
    g = graph()
    transitions = [r for r in TABLE if isinstance(r.to, Kind)]
    via = [k for r in TABLE if isinstance(r.to, lifecycle.Via) for k in r.to.kinds]
    assert len(g["status"]["edges"]) == len(transitions) + len(via)
    assert set(g["status"]["nodes"]) == {k.value for k in Kind}
    assert ("pr-open", "resume-admitted", "always", "address-review") in g["stage"]["edges"]
    assert ("queued", "resume-admitted", "always", "spec") in g["stage"]["edges"]
    assert {"queued", "spec", "pr-open", "address-review"} <= set(g["stage"]["nodes"])


def test_park_intent_at_the_gate_parks_it_for_review():
    got = decide(At(S.PLAN, GATE), Event(E.OP_PARK))
    assert got == Transition(REVIEW_PARK, S.PLAN, (EndSession(), ReleaseSlot()))
    assert operator_request(At(S.PLAN, got.status)) == PLAN
    assert ("op-park", "parked-review") in {
        (e.event, e.to) for e in next_transitions(At(S.PLAN, GATE))}


def test_an_unknown_failure_at_queued_ranked_again_is_swept_unless_written_this_pass():
    at = At(S.QUEUED, failed("unknown"))
    assert decide(at, Event(E.CANDIDATE_RANKED, Ranked(False))) == Transition(
        at.status, S.QUEUED, (SweepTombstone(),))
    assert isinstance(decide(at, Event(E.CANDIDATE_RANKED, Ranked(True))), Ignore)


def test_resume_of_a_login_hold_is_refused():
    assert isinstance(decide(At(S.SPEC, Status(Kind.HELD_LOGIN)), Event(E.OP_RESUME, ResumeChoice("", False))), Reject)


@pytest.mark.parametrize("at", [At(S.IMPLEMENT, failed("killed")),
                                At(S.PLAN, Status(Kind.CANCELED)),
                                At(S.PR_OPEN, Status(Kind.DONE))])
def test_kill_of_an_ended_task_is_refused(at):
    assert isinstance(decide(at, Event(E.OP_KILL)), Reject)


@pytest.mark.parametrize("event, status, notice", [
    (E.PR_CLOSED, failed("pr-closed"), "pr_closed"),
    (E.PR_MERGED, Status(Kind.DONE), "task_done"),
])
def test_a_pr_parked_task_ends_with_its_pr_and_notifies(event, status, notice):
    got = decide(At(S.PR_OPEN, PR), Event(event))
    assert (got.status, got.stage) == (status, S.PR_OPEN)
    assert Notify(notice) in got.actions


@pytest.mark.parametrize("dropped", [
    dict(build=lambda f: RUNNING), dict(move=(S.QUEUED, S.SPEC)),
    dict(actions=(EndSession(),)), dict(derive=(lambda f: EndSession(),)),
])
@pytest.mark.parametrize("outcome", [Ignore("no"), Reject("no")])
def test_a_row_that_decides_nothing_refuses_what_it_would_drop(outcome, dropped):
    with pytest.raises(ValueError):
        lifecycle.Row(Kind.RUNNING, E.OP_REPLY, outcome, **dropped)


@pytest.mark.parametrize("event, detail", [
    (E.OP_REPLY, LoginCode("ABCD-1234", False)), (E.OP_KILL, ResumeChoice("m", False)),
    (E.PR_FEEDBACK, Attempt(True)), (E.OP_RESUME, Ranked(True)),
    (E.SESSION_SIGNAL, Silence(round=None, note="", pane=None, login="")),
])
def test_an_event_refuses_facts_its_type_does_not_carry(event, detail):
    with pytest.raises(ValueError):
        Event(event, detail)


@pytest.mark.parametrize("event", [E.SESSION_SIGNAL, E.SESSION_DIED, E.CI_FINISHED,
                                   E.RESUME_ADMITTED, E.LAUNCH_FAILED])
def test_an_event_refuses_to_go_without_its_facts(event):
    with pytest.raises(ValueError):
        Event(event)


def test_a_fact_left_out_is_a_type_error():
    with pytest.raises(TypeError):
        Report(StageSignal("implement", "done"))


def test_a_login_code_is_in_no_repr_or_reason():
    event = Event(E.OP_LOGIN_CODE, LoginCode("ABCD-1234", False))
    assert "ABCD-1234" not in repr(event)
    for at in TASKS:
        assert "ABCD-1234" not in repr(decide(at, event)), at
