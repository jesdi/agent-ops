"""The grader's one-line feedback on the result (pr-exam-host-assist requirement 11).

Interface under test (defined here, built by the ticket), on top of test_exam_grading.py:
    GET .../ `results[].questions[]` of a missed or partial free-text question also carries
    "feedback": the grader's one line for it. A choice question and a passed question carry none.
"""
from tests.test_exam_grading import FAIL_SPEC, answers_for, box_for, graded_once, opened, result_of


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
