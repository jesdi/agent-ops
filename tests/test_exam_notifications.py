"""Telegram messages for exams, through `exams.work` with a fake notifier, and the templates.

Interface under test (defined here, built by the ticket), on top of test_exam_generation.py:
    notify(text: str) stays the notifier `exams.work` is given: one call = one Telegram message.
    The exam URL is f"{cfg.console_url}/exam/{repo}/{pr}".
    telegram.templates.render(template, **ctx) -> str, with the templates
        "exam_ready"  (repo, pr, questions: int, url, debt: bool = False)
            says the exam is ready, names `repo#pr`, "<n> question(s)" and the url;
            when debt is true it also says "debt".
        "exam_result" (repo, pr, passed: bool, score, max, url)
            says "pass" or "fail", names `repo#pr`, the score and max, and the url.
        "exam_failed" (repo, pr, reason: str)
            says the exam work failed, names `repo#pr` and the reason.
    Messages sent: ready when an exam opens (a follow-up or a defer too, a defer marked as debt);
    one result per graded submission, and after a fail the follow-up's "ready" is in it or in
    one more message; a failed job sends one failed message with the reason, once per reason.
"""
import re

from telegram.templates import render
from tests.test_exam_bypass import DEFER, SKIP, box_for as bypass_box
from tests.test_exam_generation import CONSOLE, REPO, Box, Session
from tests.test_exam_grading import FAIL_SPEC, answers_for, box_for as graded_box, graded_once, opened
from tests.test_exam_pushes import RETRY, box_for as push_box, passed_box, push

URL = f"{CONSOLE}/exam/{REPO}/240"
REF = f"{REPO}#240"


def ready(texts):
    return [t for t in texts if "ready" in t.lower()]


def test_an_exam_that_opens_sends_one_ready_message_with_repo_pr_count_and_url(tmp_path, monkeypatch):
    box = graded_box(tmp_path, monkeypatch)
    opened(box)
    [text] = box.notes
    assert "ready" in text.lower()
    assert REF in text and URL in text
    assert re.search(r"\b5 questions\b", text)


def test_a_passing_submission_sends_one_result_with_score_and_url(tmp_path, monkeypatch):
    box = graded_box(tmp_path, monkeypatch)
    body = opened(box)
    before = len(box.notes)
    graded_once(box, body)
    [text] = box.notes[before:]
    assert re.search(r"pass", text, re.I) and not re.search(r"fail", text, re.I)
    assert "4.5" in text and URL in text and REF in text


def test_a_failing_submission_sends_the_result_and_one_ready_for_the_follow_up(tmp_path, monkeypatch):
    box = graded_box(tmp_path, monkeypatch, spec=FAIL_SPEC, grades={"t0": "pass", "t1": "partial"})
    body = opened(box)
    before = len(box.notes)
    graded_once(box, body, answers=answers_for(body, wrong=("q0",)))
    new = box.notes[before:]
    assert 1 <= len(new) <= 2
    assert any(re.search(r"fail", t, re.I) and "3.5" in t and URL in t for t in new)
    [again] = ready(new)
    assert re.search(r"\b2 questions\b", again) and URL in again


def test_a_follow_up_after_a_pass_sends_a_ready_message(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[RETRY])
    before = len(box.notes)
    push(box)
    [text] = box.notes[before:]
    assert "ready" in text.lower() and REF in text and URL in text
    assert re.search(r"\b1 question\b", text)


def test_a_delta_with_nothing_new_sends_nothing(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[])
    before = len(box.notes)
    assert before == 2   # the exam's ready and the pass: nothing else yet
    push(box)
    assert len(box.notes) == before


def test_a_skip_sends_nothing(tmp_path, monkeypatch):
    box = bypass_box(tmp_path, monkeypatch, SKIP)
    box.request()
    box.work()
    assert box.notes == []
    other = bypass_box(tmp_path / "b", monkeypatch)   # the same PR with no label is announced
    other.request()
    other.work()
    assert [REF in t for t in other.notes] == [True]


def test_a_defer_sends_one_ready_message_marked_as_debt(tmp_path, monkeypatch):
    box = bypass_box(tmp_path, monkeypatch, DEFER)
    box.request()
    box.work()
    [text] = box.notes
    assert "ready" in text.lower() and "debt" in text.lower()
    assert REF in text and URL in text and re.search(r"\b5 questions\b", text)


def test_a_failed_job_sends_one_message_with_the_reason_and_its_retries_send_no_more(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch, session=Session("invalid"))
    box.request()
    box.work()
    [text] = box.notes
    assert REF in text and "no questions" in text
    for _ in range(2):
        box.request()
        box.work()
    assert box.read().json()["state"] == "error"
    assert len(box.session.calls) == 3
    assert len(box.notes) == 1


def told_once(box, reason):
    box.request()
    box.work()
    [text] = box.notes
    assert REF in text and reason in text
    box.request()
    box.work()
    assert box.read().json()["state"] == "error"
    assert len(box.notes) == 1


def test_an_unset_operator_login_sends_one_failed_message_and_its_retries_send_no_more(tmp_path, monkeypatch):
    told_once(bypass_box(tmp_path, monkeypatch, SKIP, operator=""), "operator_login")


def test_a_refused_config_sends_one_failed_message_and_its_retries_send_no_more(tmp_path, monkeypatch):
    box = bypass_box(tmp_path, monkeypatch, SKIP)
    box.github.configs = {"main": '{"bogus": 1}'}
    told_once(box, "unknown key: bogus")


def test_repeating_a_request_or_the_work_sends_nothing(tmp_path, monkeypatch):
    box = graded_box(tmp_path, monkeypatch)
    opened(box)
    box.request()
    box.work()
    box.work()
    assert len(box.notes) == 1
    passed = push_box(tmp_path / "b", monkeypatch)
    graded_once(passed, opened(passed))
    sent = len(passed.notes)
    passed.request()
    passed.work()
    passed.work()
    assert len(passed.notes) == sent and sent == 2


def test_the_exam_templates_render_what_the_operator_needs():
    text = render("exam_ready", repo=REPO, pr=240, questions=5, url=URL)
    assert "ready" in text.lower() and REF in text and "5 questions" in text and URL in text
    assert "debt" not in text.lower()
    assert "debt" in render("exam_ready", repo=REPO, pr=240, questions=5, url=URL, debt=True).lower()
    won = render("exam_result", repo=REPO, pr=240, passed=True, score=4.5, max=5, url=URL)
    lost = render("exam_result", repo=REPO, pr=240, passed=False, score=3.5, max=5, url=URL)
    assert "pass" in won.lower() and "fail" not in won.lower() and "4.5" in won and URL in won
    assert "fail" in lost.lower() and "3.5" in lost and REF in lost and URL in lost
    broke = render("exam_failed", repo=REPO, pr=240, reason="the session timed out")
    assert REF in broke and "the session timed out" in broke
