import pytest

from dispatcher.prompts import render_stage_prompt, render_triage_prompt
from dispatcher.state import Stage

CTX = dict(
    issue_number=42, issue_title="Add widget", issue_url="https://github.com/x/y/issues/42",
    repo="jesdi/portfolio_eval", branch="agent/task-42", slot=1,
    backend_port=8101, frontend_port=5201,
    verify_cmd="make e2e-slot SLOT=1", gate_cmd="make gate SLOT=1",
    spec_path="docs/specs/2026-07-14-widget-design.md",
    tickets_dir=".agent/tickets", ticket_number=2, ticket_count=5,
    ticket_path=".agent/tickets/02-widget-api.md", pr_number=12,
    reason="check-failed", labels="auto, frontend",
    tracks="- `trivial`: Rote edits.\n- `standard`: Else.",
)

STAGES = [Stage.SPEC, Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW, Stage.ADDRESS_REVIEW]


@pytest.mark.parametrize("stage", STAGES)
def test_renders_without_leftover_placeholders(stage):
    out = render_stage_prompt(stage, CTX)
    assert "$" not in out.replace("$(", "")   # only shell substitutions may keep a $
    assert "#42" in out
    assert ".agent/stage.json" in out  # every stage knows the signal protocol
    assert f'"stage": "{stage.value}"' in out


def test_spec_prompt_speaks_answers_and_review_signals():
    out = render_stage_prompt(Stage.SPEC, CTX)
    for token in ("awaiting-answers", ".agent/questionnaire.md", ".agent/prototype.html",
                  "awaiting-review", "docs: draft spec for #42",
                  "docs: spec for #42 (agent-ops)", "auto, frontend"):
        assert token in out


def test_spec_prompt_carries_the_track_list_and_signal_field():
    out = render_stage_prompt(Stage.SPEC, CTX)
    assert "- `trivial`: Rote edits." in out
    assert '"track": "<name>"' in out
    assert "approval names a track" in out


def test_plan_prompt_names_the_tickets_dir_and_spec():
    out = render_stage_prompt(Stage.PLAN, CTX)
    assert CTX["spec_path"] in out and '"artifact": ".agent/tickets"' in out
    assert ".agent/questions.md" in out and "awaiting-answers" in out


def test_implement_prompt_carries_ticket_gate_and_round_protocol():
    out = render_stage_prompt(Stage.IMPLEMENT, CTX)
    for token in (CTX["ticket_path"], CTX["spec_path"], "make gate SLOT=1",
                  '"loop": "gate"', "2 of 5"):
        assert token in out
    assert "pytest" not in out and "vitest" not in out


def test_review_prompt_carries_gates_lease_push_and_pr():
    out = render_stage_prompt(Stage.REVIEW, CTX)
    for token in ("make gate SLOT=1", "make e2e-slot SLOT=1",
                  "--force-with-lease origin agent/task-42",
                  '"loop": "review"', "awaiting-ci", "Closes #42",
                  ".agent/tickets", CTX["spec_path"]):
        assert token in out


def test_address_review_prompt_carries_reason_pr_and_gate():
    out = render_stage_prompt(Stage.ADDRESS_REVIEW, CTX)
    assert "#12" in out and "check-failed" in out and "make gate SLOT=1" in out
    assert "--force-with-lease origin agent/task-42" in out
    assert "awaiting-ci" in out and '"status": "done"' in out


def test_missing_key_raises():
    with pytest.raises(KeyError):
        render_stage_prompt(Stage.SPEC, {"issue_number": 1})


def test_render_triage_prompt():
    text = render_triage_prompt({
        "repo": "o/r",
        "decisions_path": "/triage/o-r-2026-07-30.json",
        "context_json": '{"issues": []}',
        "tracks": "- `t`: w",
    })
    assert "o/r" in text and "/triage/o-r-2026-07-30.json" in text
    assert '{"issues": []}' in text and "auto" in text and "human-required" in text


def test_render_triage_prompt_missing_var_raises():
    with pytest.raises(KeyError):
        render_triage_prompt({"repo": "o/r"})


def test_triage_prompt_lists_the_tracks_and_the_label_rule():
    out = render_triage_prompt({"repo": "o/r", "decisions_path": "/triage/x.json",
                                "context_json": "{}",
                                "tracks": "- `trivial`: Rote edits.\n- `standard`: Else."})
    assert "- `trivial`: Rote edits." in out
    assert "track:<name>" in out and "exactly one" in out
