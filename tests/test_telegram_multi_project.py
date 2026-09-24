"""Acceptance tests for ticket 04: Telegram names the project.

Black-box only: drives telegram.notify.Notifier(multi_target=...) in
dry-run mode and inspects the rendered text the same way test_telegram.py
does (render()) and the way Notifier.send's dry-run print does (capsys).
"""
import pytest

from telegram.notify import Notifier

# Every per-task template defined in telegram/templates.py's _TEMPLATES,
# excluding the box-wide ones (budget_stall, budget_resume, unit_failed,
# auth_dark) which carry no {target}/{issue}-keyed task identity.
PER_TASK_TEMPLATES = [
    "awaiting_spec_review",
    "stage_blocked",
    "pr_opened",
    "artifact_failed",
    "plan_retry",
    "session_crashed",
    "implement_started",
    "parked_question",
    "spec_parked",
    "needs_relogin",
    "resumed_for_attach",
    "task_failed",
    "review_started",
    "last_round",
    "pr_attention",
    "pr_feedback",
    "pr_updated",
    "task_done",
    "pr_closed",
]

# Box-wide templates that must render identically regardless of
# multi_target, per design.md's "Telegram names the project through ref".
BOX_WIDE_CASES = [
    ("budget_stall", dict(issue=0, title="(all tasks)", url="", note="n")),
    ("auth_dark", dict(minutes=42, host="box1")),
    ("status", dict(lines=["#1 x — implement"])),
    ("daily_digest", dict(lines=["#42 spec (awaiting review)"])),
]


def _dry_run_text(capsys, notifier, template, **ctx):
    notifier.send(template, **ctx)
    out = capsys.readouterr().out
    prefix = f"[dry-run] telegram {template}: "
    assert out.startswith(prefix), out
    return out[len(prefix):].rstrip("\n")


def _full_ctx(**overrides):
    ctx = dict(issue=7, title="Add widget", url="https://github.com/x/y/issues/7",
                note="n", target="factorial", login_url="https://claude.ai/oauth")
    ctx.update(overrides)
    return ctx


def test_multitarget_spec_ready_names_the_project(capsys):
    notifier = Notifier(multi_target=True, dry_run=True)
    text = _dry_run_text(capsys, notifier, "awaiting_spec_review",
                          **_full_ctx(issue=3, target="factorial"))
    assert text.startswith("📝 factorial#3 ")


def test_singletarget_spec_ready_is_unchanged(capsys):
    notifier = Notifier(multi_target=False, dry_run=True)
    text = _dry_run_text(capsys, notifier, "awaiting_spec_review",
                          **_full_ctx(issue=412, target="portfolio_eval"))
    assert text.startswith("📝 #412 ")


@pytest.mark.parametrize("template", PER_TASK_TEMPLATES)
def test_per_task_template_multitarget_carries_project_prefix(capsys, template):
    notifier = Notifier(multi_target=True, dry_run=True)
    text = _dry_run_text(capsys, notifier, template, **_full_ctx())
    assert "factorial#7" in text


@pytest.mark.parametrize("template", PER_TASK_TEMPLATES)
def test_per_task_template_singletarget_has_no_project_prefix(capsys, template):
    notifier = Notifier(multi_target=False, dry_run=True)
    text = _dry_run_text(capsys, notifier, template, **_full_ctx())
    assert "#7" in text
    assert "factorial#7" not in text


@pytest.mark.parametrize("template,ctx", BOX_WIDE_CASES)
def test_box_wide_templates_render_identically_in_both_modes(capsys, template, ctx):
    multi_text = _dry_run_text(capsys, Notifier(multi_target=True, dry_run=True),
                                template, **ctx)
    single_text = _dry_run_text(capsys, Notifier(multi_target=False, dry_run=True),
                                 template, **ctx)
    assert multi_text == single_text
