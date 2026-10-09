"""Acceptance tests for ticket 05 conversation restarts.

Seam: run_pass with synthetic worktrees and recording Deps. No real CLI.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import eventlog, messages
from dispatcher.models import parse_policy
from dispatcher.state import (
    PARK_HUMAN, PARK_WAKE, SessionRecord, Stage, load, write_session,
)
from telegram.inbound import Command
from tests.test_main import (
    REVIEW_PAGE_STUB, FakeGitHub, FakeNotifier, FakeSessions, cfg, gate_signal,
    make_task, patch_events, write_tickets,
)
from tests.usagefakes import session_usage


MODELS = [
    pytest.param("openai/gpt-6-astra", id="codex"),
    pytest.param("anthropic/claude-opus-5-5", id="claude"),
]
RECORDS = ["absent", "truncated", "mismatched"]
KINDS = ["wake", "attach", "plan-retry", "spec-retry"]
RECORDED_ID = "01a0fc74-9f0c-75f3-8b77-a5b5e6dfb884"
SPEC_PATH = "docs/specs/t05-approved.md"
OPERATOR_TEXT = "Check the API routes again"


class RecordingSessions(FakeSessions):
    """Record the public launch boundary, including the actual worktree."""

    def __init__(self):
        super().__init__()
        self.launches = []
        self.continuations = []

    def spawn_stage(self, target, issue, worktree, prompt, stage_name,
                    model, effort="", second=None):
        self.launches.append({
            "target": target, "issue": issue, "worktree": worktree,
            "prompt": prompt, "stage": stage_name, "model": model,
            "effort": effort,
        })
        self.alive_set.add(issue)

    def resume(self, target, issue, worktree, message, model, effort="",
               second=None, session_id=None):
        # Accept T04's session_id keyword. Any call, named or implicit,
        # violates T05's fresh-conversation path.
        self.continuations.append({
            "target": target, "issue": issue, "worktree": worktree,
            "message": message, "model": model, "session_id": session_id,
        })
        self.alive_set.add(issue)


def _arrange(tmp_path, monkeypatch, model, kind="wake", record="absent",
             stage=None, issue=281, operator_text=OPERATOR_TEXT,
             bypass=False, attach_command=False):
    assert not attach_command or (kind == "attach" and not bypass)
    c = cfg(tmp_path)
    policy = parse_policy({
        "triage": [model], "untracked": "standard",
        "tracks": {"standard": {
            "when": "Synthetic ticket 05 acceptance fixture.",
            **{name: [model] for name in ("spec", "plan", "implement", "review")},
        }},
    })
    c = replace(c, models=policy)
    provider = model.split("/", 1)[0]
    usage = session_usage(0.95 if bypass else 0.2, provider=provider)
    monkeypatch.setattr(main, "fetch_all", lambda *args, **kw: {provider: usage})
    patch_events(monkeypatch, [])

    if stage is None:
        stage = {"plan-retry": Stage.PLAN, "spec-retry": Stage.SPEC}.get(
            kind, Stage.REVIEW)
    park = (PARK_HUMAN if attach_command else PARK_WAKE
            if kind in ("wake", "attach") else "")
    wt = make_task(
        c, issue=issue, stage=stage, park=park, session=False,
        park_note="the old conversation needed operator input" if park else "",
        updated_at=datetime.now(timezone.utc).isoformat(),
        spec_path=SPEC_PATH,
        plan_retries=0 if kind == "plan-retry" else 1,
        spec_retries=0 if kind == "spec-retry" else 1,
        review_rounds=1, gate_rounds=2, e2e_rounds=3, ci_rounds=4,
        picks={name: model for name in ("spec", "plan", "implement", "review")},
        ticket_count=4,
        resume_model_override=model if bypass else "",
        resume_bypass_usage=bypass,
        hold_for_attach=kind == "attach" and not attach_command,
    )
    spec = wt / SPEC_PATH
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# Approved spec\n\n## Problem\n\n" + "scope " * 200
                    + "\n\n## Decisions\n\n" + "decision " * 200)

    if stage is Stage.IMPLEMENT:
        write_tickets(wt, 4)
    if kind == "plan-retry":
        tickets = wt / ".agent" / "tickets"
        tickets.mkdir(parents=True)
        # Preserve a numbered file and the other required parts. The
        # mechanical rejection must specifically name What to build.
        (tickets / "01-invalid.md").write_text(
            "# 01: malformed ticket\n\n**Blocked by:** None\n\n"
            "- [ ] The operator observes the completed behavior.\n\n"
            + "Synthetic behavior detail. " * 40)
        (wt / ".agent" / "review.html").write_text(REVIEW_PAGE_STUB)
        gate_signal(wt)
    elif kind == "spec-retry":
        signal = {"stage": "spec", "status": "done",
                  "artifact": SPEC_PATH, "track": "tivial"}
        (wt / ".agent" / "stage.json").write_text(json.dumps(signal))
    else:
        continued = "plan" if stage is Stage.AWAITING_PLAN_REVIEW else stage.value
        signal = {"stage": continued, "status": "working"}
        (wt / ".agent" / "stage.json").write_text(json.dumps(signal))

    record_path = Path(c.state_dir) / f"session-portfolio_eval-{issue}"
    continued = "plan" if stage is Stage.AWAITING_PLAN_REVIEW else stage.value
    wrong_stage = "review" if continued == "spec" else "spec"
    if record == "truncated":
        record_path.write_text('{"session_id":')
    elif record == "mismatched":
        write_session(c.state_dir, "portfolio_eval", issue,
                      SessionRecord(RECORDED_ID, wrong_stage))
    else:
        assert record == "absent"
        assert not record_path.exists()

    queued = messages.append(c.state_dir, "portfolio_eval", issue,
                             operator_text, "operator")
    second = messages.append(c.state_dir, "portfolio_eval", issue,
                             "Keep the public API compatible", "operator")
    if attach_command:
        patch_events(monkeypatch, [Command("attach", issue=issue)])
    sessions = RecordingSessions()
    if kind in ("plan-retry", "spec-retry"):
        # Match the existing public submitted-signal acceptance fixtures.
        # Each retry policy permits exactly one bounce for its rejection.
        sessions.alive_set.add(issue)
    notifier = FakeNotifier()
    dependencies = main.Deps(github=FakeGitHub(), sessions=sessions, notifier=notifier)
    before = load(c.state_dir, "portfolio_eval", issue)
    return c, dependencies, before, (queued, second)


def _pass(c, dependencies, before):
    main.run_pass(c, dependencies)
    assert dependencies.sessions.continuations == []
    assert len(dependencies.sessions.launches) == 1
    launch = dependencies.sessions.launches[0]
    assert (launch["target"], launch["issue"], launch["worktree"]) == (
        before.target, before.issue, before.worktree)
    assert launch["model"] == before.picks[launch["stage"]]
    return launch, load(c.state_dir, before.target, before.issue)


def _assert_delivered(c, before, queued, prompt):
    stored = {message.id: message for message in messages.all_messages(
        c.state_dir, before.target, before.issue)}
    for message in queued:
        assert message.text in prompt
        assert stored[message.id].delivered_at
    assert messages.undelivered(c.state_dir, before.target, before.issue) == []


def _assert_stage_prompt(launch, before, stage):
    assert launch["stage"] == stage
    assert f"{stage.upper()} stage of the agent-ops pipeline" in launch["prompt"]
    assert f"issue #{before.issue}" in launch["prompt"]
    assert before.branch in launch["prompt"]
    if stage != "spec":
        assert before.spec_path in launch["prompt"]


def _assert_retained_context(before, after):
    for field in ("review_rounds", "gate_rounds", "e2e_rounds", "ci_rounds",
                  "ticket_count", "track", "picks", "spec_path",
                  "worktree", "branch", "labels", "effort"):
        assert getattr(after, field) == getattr(before, field), field


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
def test_operator_wake_starts_review_with_stage_prompt_and_delivers_messages(
        tmp_path, monkeypatch, model, record):
    c, d, before, queued = _arrange(tmp_path, monkeypatch, model, record=record)
    launch, after = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "review")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert after.stage is Stage.REVIEW


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("kind", KINDS)
def test_resumed_event_explains_exactly_why_the_conversation_is_new(
        tmp_path, monkeypatch, model, record, kind):
    c, d, before, _ = _arrange(tmp_path, monkeypatch, model, kind, record)
    _pass(c, d, before)
    resumed = [entry for entry in eventlog.read_tail(c.state_dir)
               if entry["event"] == "resumed"
               and entry["target"] == before.target and entry["issue"] == before.issue]
    assert len(resumed) == 1
    wrong_stage = "review" if before.stage is Stage.SPEC else "spec"
    expected = (f"new conversation: session recorded for {wrong_stage}"
                if record == "mismatched" else "new conversation: no session recorded")
    assert resumed[0]["detail"] == expected


@pytest.mark.parametrize("model", MODELS)
def test_record_from_another_stage_never_reaches_the_fresh_review_prompt(
        tmp_path, monkeypatch, model):
    c, d, before, queued = _arrange(
        tmp_path, monkeypatch, model, record="mismatched", issue=384)
    launch, _ = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "review")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert RECORDED_ID not in launch["prompt"]


@pytest.mark.parametrize("model", MODELS)
def test_truncated_record_has_the_same_fresh_review_behavior_as_no_record(
        tmp_path, monkeypatch, model):
    c, d, before, queued = _arrange(
        tmp_path, monkeypatch, model, record="truncated", issue=384)
    launch, after = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "review")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert after.stage is Stage.REVIEW


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
def test_awaiting_plan_review_restarts_plan_with_the_operator_reply(
        tmp_path, monkeypatch, model, record):
    c, d, before, queued = _arrange(
        tmp_path, monkeypatch, model, record=record,
        stage=Stage.AWAITING_PLAN_REVIEW, issue=384,
        operator_text="Drop the second endpoint")
    launch, after = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "plan")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert after.stage is Stage.PLAN
    assert after.spec_path == before.spec_path
    _assert_retained_context(before, after)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
def test_implement_restart_keeps_its_four_tickets(
        tmp_path, monkeypatch, model, record):
    c, d, before, queued = _arrange(
        tmp_path, monkeypatch, model, record=record, stage=Stage.IMPLEMENT, issue=370)
    launch, after = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "implement")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert after.ticket_count == 4
    assert after.stage is Stage.IMPLEMENT
    _assert_retained_context(before, after)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
def test_plan_retry_starts_plan_with_format_reason_and_increments_existing_count(
        tmp_path, monkeypatch, model, record):
    c, d, before, queued = _arrange(tmp_path, monkeypatch, model, "plan-retry", record)
    launch, after = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "plan")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert "What to build" in launch["prompt"]
    assert "01-invalid.md" in launch["prompt"]
    assert after.plan_retries == before.plan_retries + 1
    assert after.spec_retries == before.spec_retries
    assert after.stage is Stage.PLAN


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
def test_spec_retry_starts_spec_with_rejection_reason_and_increments_existing_count(
        tmp_path, monkeypatch, model, record):
    c, d, before, queued = _arrange(tmp_path, monkeypatch, model, "spec-retry", record)
    launch, after = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "spec")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert "must be one of" in launch["prompt"]
    assert "'tivial'" in launch["prompt"]
    assert after.spec_retries == before.spec_retries + 1
    assert after.plan_retries == before.plan_retries
    assert after.stage is Stage.SPEC
    assert after.track == "standard"


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("attach_command", [
    pytest.param(False, id="pending-attach-wake"),
    pytest.param(True, id="operator-attach-command"),
])
def test_attach_starts_review_with_attach_message_and_new_conversation_note(
        tmp_path, monkeypatch, model, record, attach_command):
    c, d, before, queued = _arrange(
        tmp_path, monkeypatch, model, "attach", record,
        attach_command=attach_command)
    launch, _ = _pass(c, d, before)
    _assert_stage_prompt(launch, before, "review")
    _assert_delivered(c, before, queued, launch["prompt"])
    assert "attach" in launch["prompt"].lower()
    attached = [ctx for template, ctx in d.notifier.calls
                if template == "resumed_for_attach"]
    assert len(attached) == 1
    assert attached[0]["note"] == "new conversation"
    assert attached[0]["issue"] == before.issue


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("kind", KINDS)
def test_every_resume_kind_starts_fresh_without_an_implicit_or_named_resume(
        tmp_path, monkeypatch, model, record, kind):
    c, d, before, _ = _arrange(tmp_path, monkeypatch, model, kind, record)
    launch, _ = _pass(c, d, before)
    assert d.sessions.continuations == []
    assert "--continue" not in launch["prompt"]
    assert "--resume" not in launch["prompt"]
    assert "--last" not in launch["prompt"]
    assert RECORDED_ID not in launch["prompt"]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("kind", ["wake", "attach"])
def test_fresh_wake_clears_park_note_one_shot_model_and_usage_bypass(
        tmp_path, monkeypatch, model, record, kind):
    c, d, before, _ = _arrange(
        tmp_path, monkeypatch, model, kind, record, bypass=True)
    assert before.resume_model_override == model
    assert before.resume_bypass_usage is True
    assert before.park and before.park_note
    if kind == "attach":
        assert before.park == PARK_WAKE
        assert before.hold_for_attach is True
    _, after = _pass(c, d, before)
    assert after.park == ""
    assert after.park_note == ""
    assert after.resume_model_override == ""
    assert after.resume_bypass_usage is False


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("kind", KINDS)
def test_conversation_restart_preserves_loop_counters_ticket_track_and_spec_context(
        tmp_path, monkeypatch, model, record, kind):
    c, d, before, _ = _arrange(tmp_path, monkeypatch, model, kind, record)
    _, after = _pass(c, d, before)
    _assert_retained_context(before, after)
    assert after.plan_retries == before.plan_retries + (kind == "plan-retry")
    assert after.spec_retries == before.spec_retries + (kind == "spec-retry")
