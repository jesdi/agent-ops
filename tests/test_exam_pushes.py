"""New pushes through `POST /api/exam` and `exams.work` with injected fakes.

Interface under test (defined here, built by the ticket), on top of test_exam_grading.py:
    A request is reconciled against the PR's head: no caller-supplied SHA.
    open exam, head moved  -> attempts dropped, a first exam generated on the new head.
    passed exam, head moved -> a delta session runs through the same `run(request, ...)`.
        Its request file: {"mode": "delta", "repo", "pr", "head_sha", "diff": path (new diff),
         "passed_diff": path (diff of the passed head), "passed_concepts": [concept id],
         "out": path}
        The session writes `out` as an exam document; no questions means no new concept.
        Otherwise it is a follow-up exam (mode "followup") on the new concepts only.
    pr_exam.validate(exam, request) accepts an empty exam for mode "delta".
"""
import json
from pathlib import Path

import pytest

from tests.test_exam_generation import GET, HEAD, REPO
from tests.test_exam_grading import (
    LEASE, RETRY, RUBRIC, SHA, WHY, Exam, Scoring, Session, answers_for, graded_once, make_exam,
    opened, submit,
)

NEW = "def456"
OLD_DIFF = "diff --git a/lease.py b/lease.py\n+lease = acquire()\n"
NEW_DIFF = OLD_DIFF + "+retry(budget=3)\n"
SPEC = [("q0", LEASE, "choice"), ("q1", "x", "choice"), ("q2", SHA, "choice"),
        ("t0", WHY, "text"), ("t1", RUBRIC, "text")]
ALL_PASS = {"t0": "pass", "t1": "pass"}
PASSED = {LEASE, "x", SHA, WHY, RUBRIC}


class DeltaSession(Session):
    def __init__(self, *args, delta=None, **kw):
        super().__init__(*args, **kw)
        self.delta = delta  # concept ids of the new exam; None or [] = no new concept

    def __call__(self, request, *, model, timeout):
        req = json.loads(Path(request).read_text())
        if req["mode"] != "delta":
            return super().__call__(request, model=model, timeout=timeout)
        call = {"req": req, "model": model, "timeout": timeout,
                "files": {k: Path(req[k]).read_text() for k in ("diff", "passed_diff")}}
        self.calls.append(call)
        spec = [(f"d{i}", c, "choice") for i, c in enumerate(self.delta or [])]
        doc = make_exam(req["head_sha"], spec) if spec else {
            **make_exam(req["head_sha"], SPEC[:1]), "questions": [], "concepts": [], "key": {}}
        Path(req["out"]).write_text(json.dumps({**doc, "mode": "followup"}))

    def deltas(self):
        return [c for c in self.calls if c["req"]["mode"] == "delta"]


class Verdicts(Scoring):
    def validate(self, exam, request):
        if json.loads(Path(request).read_text())["mode"] == "delta":
            return []
        return super().validate(exam, request)


def box_for(tmp_path, monkeypatch, *, delta=None, spec=SPEC, grades=None):
    box = Exam(tmp_path, monkeypatch,
               session=DeltaSession(spec, grades or ALL_PASS, delta=delta))
    box.pr_exam = Verdicts()
    box.github.diff = lambda repo, n: NEW_DIFF if box.github.head == NEW else OLD_DIFF
    return box


def passed_box(tmp_path, monkeypatch, **kw):
    box = box_for(tmp_path, monkeypatch, **kw)
    body = graded_once(box, opened(box))
    assert body["state"] == "passed"
    return box


def push(box):
    box.github.head = NEW
    box.request()
    box.work()


def test_a_push_while_the_exam_is_open_replaces_it_with_one_on_the_new_head(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, grades={"t0": "pass", "t1": "partial"})
    body = opened(box)
    failed = answers_for(body, wrong=("q0", "q1"))
    assert submit(box, body, failed).status_code == 202
    box.work()
    assert box.read().json()["results"]
    push(box)
    body = box.read().json()
    assert body["state"] == "open"
    assert body["head_sha"] == NEW
    assert body["attempt"] == 1
    assert not body.get("results")
    assert [r["req"]["head_sha"] for r in box.session.calls if r["req"]["mode"] == "first"] == [
        HEAD, NEW]
    assert (REPO, NEW, "exam", "pending") in box.posted()
    assert not any(s[3] == "success" for s in box.posted())


def test_a_push_after_a_pass_with_no_new_concept_posts_success_on_the_new_head(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[])
    push(box)
    body = box.read().json()
    assert (REPO, NEW, "exam", "success") in box.posted()
    assert body["state"] == "passed"
    assert not body.get("exam") or not body["exam"].get("questions")
    assert len(box.session.deltas()) == 1
    assert not box.session.followups()


def test_a_push_after_a_pass_with_a_new_concept_opens_a_follow_up_on_that_concept_only(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[RETRY])
    push(box)
    body = box.read().json()
    assert body["state"] == "open"
    assert body["head_sha"] == NEW
    assert len(body["exam"]["questions"]) == 1
    assert (REPO, NEW, "exam", "pending") in box.posted()
    assert not any(s[1] == NEW and s[3] == "success" for s in box.posted())


def test_the_delta_request_carries_the_passed_concepts_and_both_diffs(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[])
    push(box)
    [call] = box.session.deltas()
    req = call["req"]
    assert (req["repo"], req["pr"], req["head_sha"]) == (REPO, 240, NEW)
    assert set(req["passed_concepts"]) == PASSED
    assert call["files"] == {"diff": NEW_DIFF, "passed_diff": OLD_DIFF}


def test_passing_the_follow_up_posts_success_on_the_new_head(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[RETRY])
    push(box)
    body = box.read().json()
    assert body["state"] == "open"
    body = graded_once(box, body)
    assert body["state"] == "passed"
    assert (REPO, NEW, "exam", "success") in box.posted()


def test_a_request_with_an_unchanged_head_runs_no_session(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[])
    push(box)
    assert (REPO, NEW, "exam", "success") in box.posted()
    calls, posts = len(box.session.calls), list(box.github.statuses)
    box.request()
    box.work()
    assert len(box.session.calls) == calls
    assert box.github.statuses == posts
    assert box.read().json()["state"] == "passed"


NEWER = "ghi789"


def settle(box, times=4):
    for _ in range(times):
        box.work()


def test_a_push_during_grading_replaces_the_exam_and_drops_every_old_attempt(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, grades={"t0": "pass", "t1": "partial", "t9": "pass"})
    box.session.followup_spec = [("t9", LEASE, "text")]
    first = opened(box)
    body = graded_once(box, first, answers=answers_for(first, wrong=("q0", "q1")))
    assert body["state"] == "open" and body["results"]

    def moved():
        box.session.during = None
        box.github.head = NEW
        box.request()
    box.session.during = moved
    assert submit(box, body).status_code == 202
    settle(box)
    body = box.read().json()
    assert body["state"] == "open"
    assert body["head_sha"] == NEW
    assert body["attempt"] == 1
    assert not body.get("results")
    assert (REPO, NEW, "exam", "pending") in box.posted()
    assert not any(s[3] == "success" for s in box.posted())


def test_a_push_between_the_pass_and_its_status_is_judged_on_the_new_head(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, delta=[])
    first = opened(box)
    post = box.github.post_status

    def moving(repo, sha, context, state, description):
        post(repo, sha, context, state, description)
        if state == "success" and sha == HEAD:
            box.github.head = NEW
            box.request()
    box.github.post_status = moving
    assert submit(box, first).status_code == 202
    settle(box)
    assert (REPO, HEAD, "exam", "success") in box.posted()
    assert len(box.session.deltas()) == 1
    assert (REPO, NEW, "exam", "success") in box.posted()
    assert box.read().json()["head_sha"] == NEW


def test_a_push_during_the_delta_session_is_judged_on_the_newest_head_only(tmp_path, monkeypatch):
    box = passed_box(tmp_path, monkeypatch, delta=[])
    call = box.session.__call__

    def moving(request, *, model, timeout):
        call(request, model=model, timeout=timeout)
        if json.loads(Path(request).read_text())["mode"] == "delta" and box.github.head == NEW:
            box.github.head = NEWER
            box.request()
    box.session = moving
    push(box)
    settle(box)
    assert not any(s[1] == NEW and s[3] == "success" for s in box.posted())
    assert (REPO, NEWER, "exam", "success") in box.posted()
    assert box.read().json()["head_sha"] == NEWER
