"""The grader's one-line feedback on the result (pr-exam-host-assist requirement 11).

Interface under test (defined here, built by the ticket), on top of test_exam_grading.py:
    GET .../ `attempts[].result.questions[]` of a missed or partial free-text question also carries
    "feedback": the grader's one line for it. A choice question and a passed question carry none.
"""
import json
from pathlib import Path

import pytest

from dispatcher import exam_runner
from tests.test_exam_generation import REPO
from tests.test_exam_grading import (
    FAIL_SPEC, Exam, Scoring, Session, answers_for, box_for, graded_once, opened, result_of, submit,
)
from tests.test_exam_jobs import SCRIPT


def graded(tmp_path, monkeypatch):
    # q0 wrong (choice), t0 passes, t1 partial: the grader writes feedback for both text questions
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC, grades={"t0": "pass", "t1": "partial"})
    first = opened(box)
    body = graded_once(box, first, answers=answers_for(first, wrong=("q0",)))
    return {q["id"]: q for q in result_of(body, first["attempt"])["questions"]}


def test_a_partial_free_text_question_carries_the_graders_feedback(tmp_path, monkeypatch):
    questions = graded(tmp_path, monkeypatch)
    assert questions["t1"]["points"] == 0.5
    assert questions["t1"]["feedback"] == "FB-t1"


def test_a_choice_question_and_a_passed_question_carry_no_feedback(tmp_path, monkeypatch):
    questions = graded(tmp_path, monkeypatch)
    assert questions["t1"]["feedback"] == "FB-t1"   # the grader's feedback is kept at all
    assert questions["q0"]["points"] == 0 and questions["t0"]["points"] == 1
    assert not questions["q0"].get("feedback")
    assert not questions["t0"].get("feedback")


class Malformed(Session):
    """A grading session whose feedback is a list on t0 and far too long on t1."""
    def __call__(self, request, *, model, timeout):
        super().__call__(request, model=model, timeout=timeout)
        req = json.loads(Path(request).read_text())
        if req["mode"] == "grade":
            Path(req["out"]).write_text(json.dumps({
                "t0": {"grade": "partial", "feedback": ["not", "a line"]},
                "t1": {"grade": "partial", "feedback": "y" * 5000}}))


class Installed(Scoring):
    """The pinned pr_exam.py scores, as it does on the box: it copies the grades file's feedback."""
    def score(self, exam, answers, grades, flags):
        return exam_runner.PrExam(SCRIPT).score(exam, answers, grades, flags)


@pytest.mark.skipif(not SCRIPT.is_file(), reason="the pinned pr-exam skill is not installed")
def test_malformed_feedback_never_reaches_the_result_through_the_installed_scorer(tmp_path, monkeypatch):
    box = Exam(tmp_path, monkeypatch, session=Malformed(FAIL_SPEC))
    box.pr_exam = Installed()
    first = opened(box)
    response = submit(box, first)
    assert response.status_code == 202
    box.work()
    response = box.read()
    assert response.status_code == 200
    questions = {q["id"]: q for q in result_of(response.json(), 1)["questions"]}
    assert not questions["t0"].get("feedback")
    assert questions["t1"]["feedback"] == "y" * 1000
    attempt = Path(box.cfg.state_dir) / "exams" / REPO / "240" / "attempts" / "1"
    assert json.loads((attempt / "grades.json").read_text()) == {
        "t0": {"grade": "partial", "feedback": ""}, "t1": {"grade": "partial", "feedback": "y" * 1000}}
    result = {q["id"]: q for q in json.loads((attempt / "result.json").read_text())["questions"]}
    assert (result["t0"]["feedback"], result["t1"]["feedback"]) == ("", "y" * 1000)
