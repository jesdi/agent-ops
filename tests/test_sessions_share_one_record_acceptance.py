"""Acceptance tests for ticket 07: every session reads and writes the one record."""
import json
import re
from pathlib import Path

import pytest

from dispatcher.prompts import render_stage_prompt
from dispatcher.state import Stage
from tests.test_prompts import CTX

ROOT = Path(__file__).resolve().parent.parent
ANSWERS = ".agent/review-answers.json"


def _slots_stripped(text: str) -> str:
    return re.sub(r"<!-- slot: [^>]*?-->.*?<!-- /slot -->", "", text, flags=re.DOTALL)


def test_spec_prompt_uses_questionnaire_page():
    out = render_stage_prompt(Stage.SPEC, CTX)
    for token in ("to-questionnaire", "review-page", "questionnaire mode",
                  ".agent/questionnaire.html", ".agent/questionnaire-answers.json"):
        assert token in out, token
    assert ".agent/questionnaire.md" not in out
    assert "review-page skill not installed" in out
    assert re.search(r"awaiting-answers[^\n]*\.agent/questionnaire\.html|"
                     r"\.agent/questionnaire\.html[^\n]*awaiting-answers", out)


def test_plan_prompt_reads_review_answers():
    out = render_stage_prompt(Stage.PLAN, CTX)
    assert ANSWERS in out
    assert re.search(r'"changes"[^\n]*feedback|feedback[^\n]*"changes"', out, re.I)
    assert re.search(r'"approve"[^\n]*approv|approv[^\n]*"approve"', out, re.I)
    assert re.search(r"(unmapped|unknown|cannot map|can't map)[^.]*ignore|"
                     r"ignore[^.]*(unmapped|unknown|cannot map|can't map)", out, re.I | re.S)
    assert "Corrections" in out


def test_plan_prompt_text_answer_written_with_actor_text():
    out = render_stage_prompt(Stage.PLAN, CTX)
    paras = [p for p in out.split("\n\n") if '"text"' in p and "review-answers.json" in p]
    assert paras, 'no paragraph has both "text" and review-answers.json'


@pytest.mark.parametrize("stage", [Stage.IMPLEMENT, Stage.REVIEW])
def test_implement_and_review_read_answers_before_tickets(stage):
    out = render_stage_prompt(stage, CTX)
    assert ANSWERS in out
    assert out.index(ANSWERS) < out.index(CTX["tickets_dir"])


def test_review_page_skill_is_pinned():
    pin = json.loads((ROOT / ".my-skills.json").read_text())["skills"]["review-page"]
    assert pin["version"] and pin["package"]
    assert "claude" in pin["agents"]


def test_fixture_matches_installed_template():
    installed = ROOT / ".my-skills/review-page/template.html"
    if not installed.exists():
        pytest.skip("review-page skill not installed")
    fixture = ROOT / "tests/fixtures/review-page.html"
    assert _slots_stripped(fixture.read_text()) == _slots_stripped(installed.read_text())


@pytest.mark.parametrize("doc", ["CONTEXT.md", "README.md"])
def test_docs_name_page_and_record(doc):
    text = (ROOT / doc).read_text()
    assert "review page" in text.lower()
    assert ANSWERS in text


def test_docs_note_to_questionnaire_for_fresh_box():
    text = "\n".join((ROOT / d).read_text() for d in ("CONTEXT.md", "README.md"))
    assert "to-questionnaire" in text
    assert re.search(r"fresh box|new box", text, re.I)
