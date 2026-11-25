"""Grading through the pinned `pr_exam.py score --flags` and `record` (pr-exam-host-assist
requirement 8): the flags written at submit reach the real scorer, a bypassed question counts in
neither the points nor the maximum, and an assisted question leaves an `assisted` record."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from dispatcher import exam_runner
from tests.test_exam_generation import REPO
from tests.test_exam_grading import Scoring, Session, answers_for, opened, result_of, submit
from tests.test_exam_question_bypass import GRADES, SIX, Box, bypass, without

# The pr_exam.py of the pinned skills package, by its sha256 (from the published
# @jesdi/skills tarball). Moving the pin in .my-skills.json fails this suite until it is
# re-verified here against the new package's script.
PINNED = {"0.0.103": "84732ab6a13fd1680f144d56b29741796522a0259bd95cdadae6b9ba430484b0"}
PIN = json.loads((Path(__file__).parent.parent / ".my-skills.json").read_text())["skills"]["pr-exam"]["package"]
SCRIPT = exam_runner.SCRIPT   # the installation the console runs


class Pinned(Scoring):
    """The installed skill scores and records, as it does on the box."""
    def score(self, exam, answers, grades, flags):
        return exam_runner.PrExam(SCRIPT).score(exam, answers, grades, flags)

    def record(self, records, exam, result, today):
        Path(records).mkdir(parents=True, exist_ok=True)
        exam_runner.PrExam(SCRIPT).record(records, exam, result, today)


class PinnedBox(Box):
    PR_EXAM = Pinned


def assisted_reply(box, question):
    """A finished reply in `question`'s thread while the attempt is open, as a turn stores it."""
    threads = Path(box.cfg.state_dir) / "exams" / REPO / "240" / "attempts" / "1" / "threads.json"
    threads.write_text(json.dumps({"turns": [{
        "id": "t-1", "kind": "question", "question": question, "message": "is option 1 right?",
        "status": "done", "text": "yes", "error": None, "after_submit": False, "started": "2026-11-24T00:00:00+00:00"}]}))


def test_the_installed_scorer_is_the_pinned_one():
    assert PIN in PINNED, f"the pr-exam pin moved to {PIN}: verify its pr_exam.py and add its sha256"
    if not SCRIPT.is_file() and os.environ.get("CI"):
        pytest.skip("CI installs no skills; the gate runs this against scripts/install-skills.sh's copy")
    assert SCRIPT.is_file(), "pr-exam is not installed: run scripts/install-skills.sh"
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == PINNED[PIN], (
        f"{SCRIPT} is not the pinned package {PIN}: run scripts/install-skills.sh")


@pytest.mark.skipif(not SCRIPT.is_file() and bool(os.environ.get("CI")),
                    reason="CI installs no skills; the gate runs this against the pinned copy")
def test_grading_through_the_pinned_scorer_honours_the_bypass_and_records_the_assisted_question(tmp_path, monkeypatch):
    test_the_installed_scorer_is_the_pinned_one()
    box = PinnedBox(tmp_path, monkeypatch, session=Session(SIX, grades=GRADES))
    body = opened(box)
    assert bypass(box, body, "q6").status_code == 200
    assisted_reply(box, "q2")
    assert submit(box, body, answers=without(answers_for(body), "q6")).status_code == 202
    box.work()
    after = box.read().json()
    assert after["state"] == "passed"
    result = result_of(after, 1)
    assert (result["score"], result["max"], result["passed"]) == (5, 5, True)
    questions = {q["id"]: q for q in result["questions"]}
    assert questions["q6"]["help"] == "bypassed" and "points" not in questions["q6"]
    assert questions["q6"]["explanation"] and questions["q6"]["hunk"]
    assert (questions["q2"]["help"], questions["q2"]["points"]) == ("assisted", 1)
    records = {f.name: f.read_text() for f in (Path(box.cfg.state_dir) / "exam-records" / REPO).glob("*.md")}
    states = {text.split("concept: ")[1].split("\n")[0]: text.split("state: ")[1].split("\n")[0] for text in records.values()}
    assert states["retry-budget"] == "assisted"
    assert states["lease-ownership"] == "demonstrated"
