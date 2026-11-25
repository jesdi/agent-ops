"""Each attempt keeps its own files, through `exams.work` and `GET /api/exam/{owner}/{name}/{pr}`.

Interface under test (defined here, built by the ticket), on top of test_exam_grading.py:
    <state>/exams/<owner>/<name>/<pr>/attempts/<n>/ holds the attempt's exam.json, public.json,
        answers.json, grades.json and result.json; state.json stays at the exam level.
        They stay until the exam is deleted or a new head restarts it.
    GET .../ `attempts` lists every submitted attempt of this head, oldest first:
        {"attempt", "questions": [public question], "result": the graded result | None}
        The open attempt is not listed; it is `exam`. No key and no rubric appear in it.
    An exam left in the flat layout (files at the exam level) is moved into attempts/<attempt>/
        on the first read or work, once.
"""
import json
import shutil
from pathlib import Path

from dispatcher import exams
from dispatcher.state import state_lock
from tests.test_exam_debt import Gh, request
from tests.test_exam_generation import HEAD, REPO
from tests.test_exam_grading import (
    FAIL_SPEC, answers_for, box_for, graded_once, opened, result_of, submit,
)
from tests.test_exam_pushes import NEW, Verdicts, box_for as pushes_box, push
from tests.test_exam_records import Recording

FILES = ("exam.json", "public.json", "answers.json", "grades.json", "result.json")


def exam_dir(box):
    return Path(box.cfg.state_dir) / "exams" / REPO / "240"


def attempt_dir(box, n):
    return exam_dir(box) / "attempts" / str(n)


def failed_once(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC)
    first = opened(box)
    second = graded_once(box, first, answers=answers_for(first, wrong=("q0",)))
    return box, first, second


def test_a_failed_attempt_stays_readable_once_its_follow_up_opens(tmp_path, monkeypatch):
    box, first, second = failed_once(tmp_path, monkeypatch)
    assert (first["attempt"], second["attempt"], second["state"]) == (1, 2, "open")
    for name in FILES:
        assert (attempt_dir(box, 1) / name).is_file(), name
    answers = json.loads((attempt_dir(box, 1) / "answers.json").read_text())
    assert answers["q0"] == 0 and answers["t0"] == "ANSWER-t0"
    saved = json.loads((attempt_dir(box, 1) / "exam.json").read_text())
    assert saved["head_sha"] == HEAD
    assert [q["prompt"] for q in saved["questions"]] == [
        q["prompt"] for q in first["exam"]["questions"]]
    assert saved["key"]["q0"]["answer"] == 1
    assert json.loads((attempt_dir(box, 1) / "public.json").read_text()) == first["exam"]
    grades = json.loads((attempt_dir(box, 1) / "grades.json").read_text())
    assert grades == {"t0": {"grade": "pass", "feedback": "FB-t0"},
                      "t1": {"grade": "partial", "feedback": "FB-t1"}}
    result = json.loads((attempt_dir(box, 1) / "result.json").read_text())
    assert (result["passed"], result["score"], result["max"]) == (False, 3.5, 5)
    assert (attempt_dir(box, 2) / "exam.json").is_file()
    assert (exam_dir(box) / "state.json").is_file()
    [kept] = second["attempts"]
    assert kept["attempt"] == 1
    assert [q["id"] for q in kept["questions"]] == ["q0", "q1", "q2", "t0", "t1"]
    assert kept["questions"][3]["prompt"] == "PROMPT-t0"
    assert kept["result"] == result_of(second, 1)
    assert kept["result"]["passed"] is False


def test_the_exam_view_lists_failed_attempts_without_any_key(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC)
    first = opened(box)
    for secret in ("RUBRIC-", "EXPL-", "HUNK-", '"key"'):
        assert secret not in box.read().text
    graded_once(box, first, answers=answers_for(first, wrong=("q0",)))
    text = box.read().text
    assert "attempts" in box.read().json()
    for secret in ("RUBRIC-PASS", "RUBRIC-PARTIAL", "EXPL-q1", "EXPL-q2", "EXPL-t0", '"key"'):
        assert secret not in text
    for q in box.read().json()["attempts"][0]["questions"]:
        assert not set(q) & {"answer", "rubric", "explanation", "hunk"}


def test_a_follow_ups_request_carries_the_prompts_of_the_attempt_it_follows(tmp_path, monkeypatch):
    box, first, second = failed_once(tmp_path, monkeypatch)
    third = graded_once(box, second, answers=answers_for(
        second, wrong=[q["id"] for q in second["exam"]["questions"]]))
    assert third["attempt"] == 3
    one, two = (c["req"] for c in box.session.followups())
    assert one["prior_prompts"] == [q["prompt"] for q in first["exam"]["questions"]]
    assert two["prior_prompts"] == [q["prompt"] for q in second["exam"]["questions"]]
    assert (attempt_dir(box, 1) / "exam.json").is_file()
    assert (attempt_dir(box, 2) / "exam.json").is_file()
    assert [a["attempt"] for a in third["attempts"]] == [1, 2]


def test_a_new_head_removes_every_attempts_files_and_numbering_starts_over(tmp_path, monkeypatch):
    box = pushes_box(tmp_path, monkeypatch, grades={"t0": "pass", "t1": "partial"})
    body = opened(box)
    body = graded_once(box, body, answers=answers_for(body, wrong=("q0", "q1")))
    assert body["attempt"] == 2
    push(box)
    body = box.read().json()
    assert (body["attempt"], body["head_sha"], body["state"]) == (1, NEW, "open")
    assert not body.get("attempts")
    assert (exam_dir(box) / "attempts").is_dir(), "new head must create attempts/1"
    assert sorted(p.name for p in (exam_dir(box) / "attempts").iterdir()) == ["1"]
    assert sorted(p.name for p in attempt_dir(box, 1).iterdir()) == ["exam.json", "public.json"]
    assert json.loads((attempt_dir(box, 1) / "exam.json").read_text())["head_sha"] == NEW


def test_fail_pass_empty_delta_and_deletion_preserve_states_statuses_and_records(
        tmp_path, monkeypatch):
    box = pushes_box(tmp_path, monkeypatch, spec=FAIL_SPEC,
                     grades={"t0": "pass", "t1": "partial"}, delta=[])
    box.pr_exam = Recording()
    box.pr_exam.validate = Verdicts().validate
    diff = box.github.diff
    box.github = Gh()
    box.github.diff = diff
    first = opened(box)
    second = graded_once(box, first, answers=answers_for(first, wrong=("q0",)))
    assert (second["state"], second["attempt"]) == ("open", 2)
    assert result_of(second, 1)["passed"] is False
    assert {s[3] for s in box.posted()} == {"pending"}
    assert len(box.pr_exam.calls) == 1
    failed_records = box.pr_exam.calls[0]["result_questions"]
    assert sum(q["points"] < 1 for q in failed_records) == 2

    passed = graded_once(box, second)
    assert passed["state"] == "passed" and result_of(passed, 2)["passed"] is True
    assert (REPO, HEAD, "exam", "success") in box.posted()
    assert len(box.pr_exam.calls) == 2
    records_dir = Path(box.cfg.state_dir) / "exam-records" / REPO
    records = {p.name: p.read_bytes() for p in records_dir.glob("*.md")}
    assert len(records) == 7
    assert sum(b"state: misconception" in v for v in records.values()) == 2
    assert sum(b"state: demonstrated" in v for v in records.values()) == 5

    push(box)
    body = box.read().json()
    assert (body["state"], body["head_sha"]) == ("passed", NEW)
    assert (REPO, NEW, "exam", "success") in box.posted()
    assert len(box.session.deltas()) == 1 and len(box.session.followups()) == 1
    assert len(box.pr_exam.calls) == 2
    assert {p.name: p.read_bytes() for p in records_dir.glob("*.md")} == records
    # Both graded results survive the empty delta, alongside their original exams.
    for n in (1, 2):
        for name in FILES:
            assert (attempt_dir(box, n) / name).is_file(), f"attempt {n}: {name}"
        result = json.loads((attempt_dir(box, n) / "result.json").read_text())
        assert result["passed"] is (n == 2)
    assert [a["attempt"] for a in body["attempts"]] == [1, 2]

    box.github.merge(REPO, 240)
    request(box, REPO, 240)
    assert box.read().status_code == 404
    assert not exam_dir(box).exists()
    assert {p.name: p.read_bytes() for p in records_dir.glob("*.md")} == records
    assert len(box.pr_exam.calls) == 2


def test_an_exam_left_in_the_flat_layout_is_migrated_once_and_graded_with_its_answers(
        tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert submit(box, body).status_code == 202
    root, new = exam_dir(box), attempt_dir(box, 1)
    if new.is_dir():                                   # simulate the box as it was before deploy
        for f in new.iterdir():
            shutil.move(str(f), root / f.name)
        shutil.rmtree(root / "attempts")
    assert (root / "exam.json").is_file() and (root / "answers.json").is_file()
    # The first console read must migrate, before the worker starts grading.
    response = box.read()
    assert response.status_code == 200 and response.json()["state"] == "grading"
    for name in ("exam.json", "public.json", "answers.json"):
        assert (new / name).is_file(), f"first read must migrate {name}"
        assert not (root / name).exists(), name
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in new.iterdir()}
    assert box.read().json() == response.json()
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in new.iterdir()} == before
    box.work()
    final = box.read().json()
    assert final["state"] == "passed"
    assert json.loads(box.session.graded()[0]["files"]["answers"]) == {
        "t0": "ANSWER-t0", "t1": "ANSWER-t1"}
    for name in ("exam.json", "public.json", "answers.json", "grades.json", "result.json"):
        assert (new / name).is_file(), name
        assert not (root / name).exists(), name
    before = {p.name: p.stat().st_mtime_ns for p in new.iterdir()}
    box.read()
    box.work()
    assert {p.name: p.stat().st_mtime_ns for p in new.iterdir()} == before
    assert len(box.session.graded()) == 1


def flatten(box, owners):
    """Put the box back as the flat layout left it: each file at the exam level, taken from
    the attempt `owners` names for it, attempts/ gone, and no feedback in state.json."""
    root = exam_dir(box)
    for name, n in owners.items():
        shutil.move(str(attempt_dir(box, n) / name), root / name)
    shutil.rmtree(root / "attempts")
    doc = json.loads((root / "state.json").read_text())
    for result in doc["results"]:
        for q in result["questions"]:
            q.pop("feedback", None)
    (root / "state.json").write_text(json.dumps(doc))


def names(box, n):
    return sorted(p.name for p in attempt_dir(box, n).iterdir())


def test_a_flat_exam_with_its_follow_up_open_keeps_the_failed_submission_with_the_failed_attempt(
        tmp_path, monkeypatch):
    box, _, _ = failed_once(tmp_path, monkeypatch)
    flatten(box, {"exam.json": 2, "public.json": 2,
                  "answers.json": 1, "grades.json": 1, "result.json": 1})
    body = box.read().json()
    assert (body["state"], body["attempt"]) == ("open", 2)
    assert names(box, 2) == ["exam.json", "public.json"]
    assert names(box, 1) == ["answers.json", "grades.json", "result.json"]
    assert json.loads((attempt_dir(box, 1) / "answers.json").read_text())["q0"] == 0
    failed = {q["id"]: q for q in result_of(body, 1)["questions"]}
    assert failed["t1"]["feedback"] == "FB-t1"   # the summary is backfilled from result.json
    passed = graded_once(box, body)
    assert passed["state"] == "passed" and result_of(passed, 2)["passed"] is True
    assert json.loads((attempt_dir(box, 2) / "answers.json").read_text()) == answers_for(body)
    assert json.loads((attempt_dir(box, 1) / "result.json").read_text())["passed"] is False


def test_a_flat_exam_grading_its_follow_up_is_graded_with_the_follow_ups_answers(
        tmp_path, monkeypatch):
    box, _, second = failed_once(tmp_path, monkeypatch)
    assert submit(box, second).status_code == 202
    flatten(box, {"exam.json": 2, "public.json": 2, "answers.json": 2,
                  "grades.json": 1, "result.json": 1})
    assert box.read().json()["state"] == "grading"
    assert names(box, 2) == ["answers.json", "exam.json", "public.json"]
    assert names(box, 1) == ["grades.json", "result.json"]
    box.work()
    assert box.read().json()["state"] == "passed"
    assert json.loads((attempt_dir(box, 2) / "result.json").read_text())["passed"] is True
    assert json.loads((attempt_dir(box, 1) / "result.json").read_text())["passed"] is False


def test_a_flat_passed_exam_moves_whole_into_its_attempt_and_its_summary_gains_the_feedback(
        tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    assert graded_once(box, opened(box))["state"] == "passed"
    flatten(box, {name: 1 for name in FILES})
    body = box.read().json()
    assert names(box, 1) == sorted(FILES)
    questions = {q["id"]: q for q in result_of(body, 1)["questions"]}
    assert questions["t1"]["feedback"] == "FB-t1"
    assert "feedback" not in questions["t0"]


def test_a_failed_attempt_stays_in_the_view_while_its_follow_up_is_generated(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC)
    first = opened(box)
    session, seen = box.session, []

    def peek(request, **kw):
        if json.loads(Path(request).read_text())["mode"] == "followup":
            seen.append(box.read().json())
        return session(request, **kw)
    box.session = peek
    assert submit(box, first, answers=answers_for(first, wrong=("q0",))).status_code == 202
    box.work()
    [during] = seen
    assert (during["state"], during["attempt"], during["exam"]) == ("generating", 1, None)
    [failed] = during["attempts"]
    assert failed["attempt"] == 1 and failed["result"]["passed"] is False
    assert [q["id"] for q in failed["questions"]] == ["q0", "q1", "q2", "t0", "t1"]


def test_the_view_reads_every_attempt_file_under_the_exams_lock(tmp_path, monkeypatch):
    box, _, _ = failed_once(tmp_path, monkeypatch)
    real, held = exams.public, []

    def public(state_dir, repo, pr, attempt):
        try:
            with state_lock(state_dir, exams.LOCK, timeout=0):
                held.append(False)
        except TimeoutError:
            held.append(True)
        return real(state_dir, repo, pr, attempt)
    monkeypatch.setattr(exams, "public", public)
    body = box.read().json()
    assert [a["attempt"] for a in body["attempts"]] == [1] and body["exam"]
    assert held == [True, True]


def test_a_flat_file_of_an_errored_first_grading_stays_with_attempt_one():
    errored = {"attempt": 1, "results": [], "grading": {"attempt": 1}}
    assert {name: exams._flat_owner(errored, name) for name in FILES} == dict.fromkeys(FILES, 1)


def test_a_flat_result_of_an_attempt_with_no_summary_backfills_nothing(tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"questions": [{"id": "t1", "points": 0, "feedback": "FB"}]}))
    doc = {"results": [{"attempt": 2, "questions": [{"id": "t1", "points": 0}]}]}
    assert exams._backfill(doc, 1, result) is False
    assert "feedback" not in doc["results"][0]["questions"][0]
