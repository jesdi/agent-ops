"""Render versioned stage prompt templates (prompts/*.md, $var placeholders)."""
from __future__ import annotations

from pathlib import Path
from string import Template

from dispatcher.state import Stage

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_TEMPLATE_FILES = {
    Stage.SPEC: "spec.md",
    Stage.PLAN: "plan.md",
    Stage.IMPLEMENT: "implement.md",
}


def render_stage_prompt(stage: Stage, ctx: dict) -> str:
    text = (PROMPTS_DIR / _TEMPLATE_FILES[stage]).read_text()
    return Template(text).substitute(ctx)  # strict: KeyError on missing vars
