"""Mechanical artifact sanity checks — no LLM judgment. A failed check
marks the stage failed instead of propagating garbage downstream."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MIN_BYTES = 1500

# Spec drafts follow the target repo's brainstorming skill: a title plus
# at least two H2 sections. Plans follow writing-plans: a Goal line and
# at least one "### Task N:" heading.
SPEC_PATTERNS = [r"^# .+", r"^## .+", r"^## .+[\s\S]*^## .+"]
PLAN_PATTERNS = [r"\*\*Goal:\*\*", r"^### Task \d+:"]


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""


def _check(path: str | Path, patterns: list[str]) -> CheckResult:
    p = Path(path)
    if not p.exists():
        return CheckResult(False, f"artifact missing: {p}")
    text = p.read_text(errors="replace")
    if len(text.encode()) < MIN_BYTES:
        return CheckResult(False, f"artifact too small (<{MIN_BYTES}B): {p}")
    for pat in patterns:
        if not re.search(pat, text, re.MULTILINE):
            return CheckResult(False, f"required heading/pattern not found ({pat}): {p}")
    return CheckResult(True)


def check_spec(path: str | Path) -> CheckResult:
    return _check(path, SPEC_PATTERNS)


def check_plan(path: str | Path) -> CheckResult:
    return _check(path, PLAN_PATTERNS)
