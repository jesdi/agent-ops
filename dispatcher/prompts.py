"""Render versioned stage prompt templates (prompts/*.md, $var placeholders)."""
from __future__ import annotations

import re
from pathlib import Path
from string import Template

from dispatcher.state import Stage

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_TEMPLATE_FILES = {
    Stage.SPEC: "spec.md",
    Stage.PLAN: "plan.md",
    Stage.IMPLEMENT: "implement.md",
    Stage.REVIEW: "review.md",
    Stage.ADDRESS_REVIEW: "address_review.md",
}

# review.md/address_review.md's e2e sections (Signals bullet, the step
# itself, and review.md's Verification line) vary by whether the target has
# a verify_cmd. Each variant lives in one small fragment file with
# ===KEY=== sections, so the mode-specific wording lives in one place
# instead of branching in Python per section.
_E2E_FRAGMENTS = {
    Stage.REVIEW: {True: "e2e_review.md", False: "no_e2e.md"},
    Stage.ADDRESS_REVIEW: {True: "e2e_address_review.md", False: "no_e2e.md"},
}
_FRAGMENT_MARKER = re.compile(r"(?m)^===(\w+)===\n")


def _load_fragment(name: str) -> dict:
    parts = _FRAGMENT_MARKER.split((PROMPTS_DIR / name).read_text())
    return {parts[i]: parts[i + 1].rstrip("\n") for i in range(1, len(parts), 2)}


def render_stage_prompt(stage: Stage, ctx: dict) -> str:
    text = (PROMPTS_DIR / _TEMPLATE_FILES[stage]).read_text()
    if stage in _E2E_FRAGMENTS:
        frag = _load_fragment(_E2E_FRAGMENTS[stage][bool(ctx["verify_cmd"])])
        signal = frag.get("SIGNAL", "")
        ctx = {**ctx,
               "e2e_signal": Template(f"{signal}\n").substitute(ctx) if signal else "",
               "e2e_step": Template(frag.get("STEP", "")).substitute(ctx),
               "e2e_verification": Template(frag.get("VERIFICATION", "")).substitute(ctx)}
    artifacts = (PROMPTS_DIR / "artifacts.md").read_text()
    return Template(text).substitute(ctx) + "\n\n" + artifacts


def render_triage_prompt(ctx: dict) -> str:
    text = (PROMPTS_DIR / "triage.md").read_text()
    return Template(text).substitute(ctx)  # strict: KeyError on missing vars
