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


def test_implement_prompt_uses_ci_protocol():
    ctx = dict(issue_number=1, issue_title="t", issue_url="u", repo="o/r",
               branch="agent/task-1", slot=0, backend_port=8100,
               frontend_port=5200, verify_cmd="make e2e-remote", spec_path="")
    text = render_stage_prompt(Stage.IMPLEMENT, ctx)
    assert "awaiting-ci" in text and "run_id" in text
    assert "gh workflow run" in text
    assert "e2e-slot" not in text


def test_address_review_prompt_renders():
    text = render_stage_prompt(Stage.ADDRESS_REVIEW, dict(
        issue_number=7, issue_title="Add widget", issue_url="u",
        repo="o/r", branch="agent/task-7", slot=0, backend_port=8100,
        frontend_port=5200, verify_cmd="make e2e", spec_path="",
        pr_number=12))
    assert "PR #12" in text or "pull request #12" in text
    assert '"stage": "address-review"' in text
    assert "awaiting-ci" in text and '"status": "done"' in text
