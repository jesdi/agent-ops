"""Pushes that race the exam's jobs, through `POST /api/exam`, `exams.work` and the GET:
during the delta success post, during the follow-up session, and while a reconcile reads
the head. The newest head is processed and replaced attempts are cleared."""
import json
from pathlib import Path

from tests.test_exam_generation import HEAD, REPO
from tests.test_exam_grading import FAIL_SPEC, answers_for, opened, submit
from tests.test_exam_pushes import NEW, NEWER, box_for, passed_box, push, settle


def test_a_push_while_the_delta_success_is_posted_is_judged_on_the_newest_head(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[])
    post = box.github.post_status

    def moving(repo, sha, context, state, description):
        post(repo, sha, context, state, description)
        if state == "success" and sha == NEW:
            box.github.head = NEWER
            box.request()
    box.github.post_status = moving
    push(box)
    settle(box)
    assert (REPO, NEW, "exam", "success") in box.posted()
    assert (REPO, NEWER, "exam", "success") in box.posted()
    body = box.read().json()
    assert (body["state"], body["head_sha"]) == ("passed", NEWER)
    assert len(box.session.deltas()) == 2


def test_a_push_during_the_follow_up_session_replaces_the_failed_attempts(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC, grades={"t0": "pass", "t1": "partial"})
    first = opened(box)
    call = box.session.__call__

    def moving(request, *, model, timeout):
        call(request, model=model, timeout=timeout)
        if json.loads(Path(request).read_text())["mode"] == "followup" and box.github.head == HEAD:
            box.github.head = NEW
            box.request()
    box.session = moving
    assert submit(box, first, answers=answers_for(first, wrong=("q0",))).status_code == 202
    settle(box)
    body = box.read().json()
    assert (body["state"], body["head_sha"], body["attempt"]) == ("open", NEW, 1)
    assert body["results"] == []
    assert len(body["exam"]["questions"]) == 5
    assert not any(s[3] == "success" for s in box.posted())


def test_a_request_made_while_the_head_is_read_is_reconciled_again(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    opened(box)
    pr, raced = box.github.pr, []

    def racing(repo, n):
        doc = pr(repo, n)   # GitHub answers with the old head, then the push lands
        if not raced:
            raced.append(1)
            box.github.head = NEW
            box.request()
        return doc
    box.request()
    box.github.pr = racing
    box.work()
    assert box.read().json()["head_sha"] == HEAD
    box.github.pr = pr
    box.work()
    body = box.read().json()
    assert (body["state"], body["head_sha"]) == ("open", NEW)
