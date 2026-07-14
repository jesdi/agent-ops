import pytest

from dispatcher.prompts import render_stage_prompt
from dispatcher.state import Stage

CTX = dict(
    issue_number=42, issue_title="Add widget", issue_url="https://github.com/x/y/issues/42",
    repo="jesdi/portfolio_eval", branch="agent/task-42", slot=1,
    backend_port=8101, frontend_port=5201,
    verify_cmd="make e2e-slot SLOT=1",
    spec_path="docs/superpowers/specs/2026-07-14-widget-design.md",
)


@pytest.mark.parametrize("stage", [Stage.SPEC, Stage.PLAN, Stage.IMPLEMENT])
def test_renders_without_leftover_placeholders(stage):
    out = render_stage_prompt(stage, CTX)
    assert "$issue_number" not in out and "${" not in out
    assert "#42" in out or "42" in out
    assert ".agent/stage.json" in out  # every stage knows the signal protocol


def test_plan_prompt_references_spec_and_self_review():
    out = render_stage_prompt(Stage.PLAN, CTX)
    assert CTX["spec_path"] in out
    assert "self-review" in out.lower()
    assert ".agent/plan.md" in out


def test_implement_prompt_has_verify_and_pr():
    out = render_stage_prompt(Stage.IMPLEMENT, CTX)
    assert "make e2e-slot SLOT=1" in out
    assert "Closes #42" in out
    assert ".agent/plan.md" in out


def test_missing_key_raises():
    with pytest.raises(KeyError):
        render_stage_prompt(Stage.SPEC, {"issue_number": 1})
