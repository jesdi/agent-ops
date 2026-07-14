from pathlib import Path

from dispatcher.artifacts import check_plan, check_spec

GOOD_SPEC = (
    "# widget frobnicator — design\n\n"
    "## Problem\n\n" + ("Detail about the problem. " * 30) + "\n\n"
    "## Decisions\n\n" + ("A decision and its rationale. " * 30) + "\n"
)

GOOD_PLAN = (
    "# Widget Frobnicator Implementation Plan\n\n"
    "**Goal:** Frobnicate widgets end to end.\n\n"
    "### Task 1: Frobnicator core\n\n" + ("Step detail. " * 60) + "\n"
    "### Task 2: Wire-up\n\n" + ("Step detail. " * 60) + "\n"
)


def test_spec_ok(tmp_path: Path):
    p = tmp_path / "spec.md"
    p.write_text(GOOD_SPEC)
    assert check_spec(p).ok


def test_spec_missing(tmp_path: Path):
    r = check_spec(tmp_path / "nope.md")
    assert not r.ok and "missing" in r.reason


def test_spec_too_small(tmp_path: Path):
    p = tmp_path / "spec.md"
    p.write_text("# tiny\n\n## Problem\n\n## Decisions\n")
    r = check_spec(p)
    assert not r.ok and "small" in r.reason


def test_spec_missing_headings(tmp_path: Path):
    p = tmp_path / "spec.md"
    p.write_text("x" * 5000)
    r = check_spec(p)
    assert not r.ok and "heading" in r.reason


def test_plan_ok(tmp_path: Path):
    p = tmp_path / "plan.md"
    p.write_text(GOOD_PLAN)
    assert check_plan(p).ok


def test_plan_missing_tasks(tmp_path: Path):
    p = tmp_path / "plan.md"
    p.write_text("**Goal:** something\n" + "x" * 5000)
    r = check_plan(p)
    assert not r.ok and "heading" in r.reason
