"""Failures between the steps of a graded attempt, through `POST .../answers`,
`exams.work` and the GET: a GitHub lookup that fails after the follow-up session, and a
status post that fails after the records were written."""
import subprocess

from tests.test_exam_generation import HEAD, REPO
from tests.test_exam_grading import FAIL_SPEC, answers_for, opened, submit
from tests.test_exam_pushes import box_for
from tests.test_exam_records import PASS_ALL, Box as RecordsBox


def test_a_failed_lookup_after_the_follow_up_never_grades_old_answers_against_it(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC, grades={"t0": "pass", "t1": "partial"})
    first = opened(box)
    pr, failed = box.github.pr, []

    def flaky(repo, n):
        if box.session.followups() and not failed:
            failed.append(1)
            raise subprocess.CalledProcessError(1, "gh")
        return pr(repo, n)
    box.github.pr = flaky
    assert submit(box, first, answers=answers_for(first, wrong=("q0",))).status_code == 202
    box.work()
    assert failed
    body = box.read().json()
    assert body["state"] == "generating" and [r["attempt"] for r in body["results"]] == [1]
    graded = len(box.session.graded())
    box.work()
    body = box.read().json()
    assert (body["state"], body["attempt"]) == ("open", 2)
    assert len(body["exam"]["questions"]) == 2
    assert len(box.session.graded()) == graded
    assert [r["attempt"] for r in body["results"]] == [1]


def test_a_failed_status_post_is_retried_without_writing_the_records_again(tmp_path, monkeypatch):
    box = RecordsBox(tmp_path, monkeypatch, grades=PASS_ALL)
    body = box.open(REPO, 240)
    post, failures = box.github.post_status, []

    def flaky(repo, sha, context, state, description):
        if state == "success" and not failures:
            failures.append(1)
            raise subprocess.TimeoutExpired("gh", 120)
        post(repo, sha, context, state, description)
    box.github.post_status = flaky
    box.grade(REPO, 240, body)
    box.work()
    assert failures and box.read().json()["state"] == "passed"
    assert not any(s[3] == "success" for s in box.posted())
    box.work()
    assert (REPO, HEAD, "exam", "success") in box.posted()
    assert len(box.pr_exam.calls) == 1
    assert len(box.records(REPO)) == 5
    box.work()
    assert [s for s in box.posted() if s[3] == "success"] == [(REPO, HEAD, "exam", "success")]


def test_a_retry_that_raises_keeps_the_failure_told_once(tmp_path, monkeypatch):
    from tests.test_exam_generation import Box, Session
    box = Box(tmp_path, monkeypatch, session=Session("invalid"))
    box.request()
    box.work()
    pr, raised = box.github.pr, []

    def flaky(repo, n):
        if len(box.session.calls) == 2 and not raised:
            raised.append(1)
            raise subprocess.CalledProcessError(1, "gh")
        return pr(repo, n)
    box.github.pr = flaky
    box.request()
    try:
        box.work()
    except subprocess.CalledProcessError:
        pass
    assert raised
    box.request()
    box.work()
    assert box.read().json()["state"] == "error" and len(box.session.calls) == 3
    assert len(box.notes) == 1


def test_a_failed_config_lookup_after_a_pass_still_gets_its_success_posted(tmp_path, monkeypatch):
    box = RecordsBox(tmp_path, monkeypatch, grades=PASS_ALL)
    body = box.open(REPO, 240)
    file, failed = box.github.file, []

    def flaky(repo, ref, path):
        if box.session.graded() and not failed:
            failed.append(1)
            raise subprocess.CalledProcessError(1, "gh")
        return file(repo, ref, path)
    box.github.file = flaky
    box.grade(REPO, 240, body)
    box.work()
    assert failed and box.read().json()["state"] == "passed"
    assert not any(s[3] == "success" for s in box.posted())
    box.work()
    assert (REPO, HEAD, "exam", "success") in box.posted()
    assert len(box.records(REPO)) == 5
