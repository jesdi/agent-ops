"""The pr-exam skills pin carries `pr_exam.py context` and `score --flags`, and the README guards it."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRST_PACKAGE, FIRST_VERSION = (0, 0, 103), (0, 1, 1)   # `@jesdi/skills` 0.0.103 ships `pr-exam` 0.1.1


def numbers(version):
    return tuple(int(n) for n in version.split("."))


def test_the_pin_is_at_or_past_the_first_release_with_context_and_score_flags():
    pin = json.loads((ROOT / ".my-skills.json").read_text())["skills"]["pr-exam"]
    assert numbers(pin["package"]) >= FIRST_PACKAGE
    assert numbers(pin["version"]) >= FIRST_VERSION


def test_the_readme_says_never_to_pin_back_past_that_release():
    readme = (ROOT / "README.md").read_text()
    assert re.search(r"never[^.\n]*pin[^.\n]*(back|past|before)[^.\n]*0\.0\.103|0\.0\.103[^.\n]*never[^.\n]*(back|past|before)",
                     readme, re.I), "README must say the pr-exam pin never moves back past @jesdi/skills 0.0.103"
