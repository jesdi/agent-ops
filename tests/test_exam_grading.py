"""Exam grading through `POST .../answers`, `exams.work` with injected fakes, then the GET.

Interface under test (defined here, built by the ticket), on top of test_exam_generation.py:
    POST /api/exam/{owner}/{name}/{pr}/answers
        {"attempt": int, "head_sha": str, "answers": {question id: int (choice index) | str}}
        200 accepted; 409 for a repeated attempt, an earlier attempt or another head_sha;
        422 for a missing answer. A 409 or 422 changes nothing.
    GET .../ `attempt` is the current attempt; `results` lists graded attempts, oldest first:
        {"attempt", "passed", "score", "max", "questions": [{"id", "concept", "points", ...}]}
        a question with points < 1 also carries "explanation" and "hunk"; a passed one
        carries nothing of the key.
    The same `run(request, *, model, timeout)` runs the grading session. Its request file:
        {"mode": "grade", "repo", "pr", "head_sha", "diff": path,
         "questions": path -> [{"id", "prompt", "rubric"}] (free text only),
         "answers": path -> {id: answer} (free text only), "out": path}
    and the session writes `out` as {id: {"grade": "pass"|"partial"|"fail", "feedback": str}}.
    A follow-up is generated with the generation request: mode "followup", `only_concepts`.
    pr_exam.score(exam: Path, answers: Path, grades: Path) -> dict   (what `pr_exam.py score`
        prints: passed, score, max, questions, missed); answers holds every answer, grades
        holds the text grades.
"""
import json
import subprocess
from pathlib import Path

import pytest

from tests.test_exam_generation import DIFF, GET, HEAD, HEADERS, NOW, REPO, Box, PrExam

ANSWERS = f"{GET}/answers"
LEASE, RETRY, SHA, WHY, RUBRIC = ("lease-ownership", "retry-budget", "base-sha-recheck",
                                  "why-lease", "rubric-fixture")
KINDS = {LEASE: "concept", RETRY: "concept", SHA: "architecture", WHY: "why", RUBRIC: "concept",
         "x": "concept"}


def key_entry(qid, type_):
    entry = {"explanation": f"EXPL-{qid}", "hunk": f"HUNK-{qid}"}
    if type_ == "choice":
        return {**entry, "answer": 1}
    return {**entry, "rubric": {"pass": f"RUBRIC-PASS-{qid}", "partial": f"RUBRIC-PARTIAL-{qid}"}}


def make_exam(head, spec):
    """spec: [(question id, concept, type)]"""
    qs = [{"id": q, "concept": c, "type": t, "prompt": f"PROMPT-{q}",
           **({"options": ["opt a", "opt b"]} if t == "choice" else {})} for q, c, t in spec]
    return {"version": 1, "mode": "first", "repo": REPO, "pr": 240, "head_sha": head,
            "concepts": [{"id": c, "title": c, "kind": KINDS[c]} for _, c, _ in spec],
            "questions": qs, "key": {q: key_entry(q, t) for q, _, t in spec}}


FIVE = [("q0", LEASE, "choice"), ("q1", RETRY, "choice"), ("q2", SHA, "choice"),
        ("t0", WHY, "text"), ("t1", RUBRIC, "text")]
# 70%: q0 wrong (lease), t1 partial (base SHA recheck) -> 1 + 1 + 1 + 0.5 over 5, minus q0
FAIL_SPEC = [("q0", LEASE, "choice"), ("q1", RETRY, "choice"), ("q2", "x", "choice"),
             ("t0", WHY, "text"), ("t1", SHA, "text")]
POINTS = {"pass": 1, "partial": 0.5, "fail": 0}


class Session:
    def __init__(self, spec=FIVE, grades=None):
        self.spec, self.calls, self.during, self.grade_error = spec, [], None, None
        self.grades = grades or {"t0": "pass", "t1": "partial"}
        self.followup_spec = None

    def __call__(self, request, *, model, timeout):
        req = json.loads(Path(request).read_text())
        self.calls.append({"req": req, "model": model, "timeout": timeout})
        mode = req["mode"]
        if mode == "grade":
            call = self.calls[-1]
            call["files"] = {k: Path(req[k]).read_text() for k in ("diff", "questions", "answers")}
            if self.during:
                self.during()
            if self.grade_error:
                raise self.grade_error
            qs = json.loads(call["files"]["questions"])
            Path(req["out"]).write_text(json.dumps(
                {q["id"]: {"grade": self.grades[q["id"]], "feedback": f"FB-{q['id']}"} for q in qs}))
            return
        if mode == "followup":
            spec = self.followup_spec or [(f"f{len(self.calls)}{i}", c, "choice")
                                          for i, c in enumerate(req["only_concepts"])]
        else:
            spec = self.spec
        Path(req["out"]).write_text(json.dumps(make_exam(req["head_sha"], spec)))

    def graded(self):
        return [c for c in self.calls if c["req"]["mode"] == "grade"]

    def followups(self):
        return [c for c in self.calls if c["req"]["mode"] == "followup"]


class Scoring(PrExam):
    def score(self, exam, answers, grades):
        exam, answers, grades = (json.loads(Path(p).read_text()) for p in (exam, answers, grades))
        kinds = {c["id"]: c["kind"] for c in exam["concepts"]}
        out, missed, total, why_ok = [], [], 0, True
        for q in exam["questions"]:
            k = exam["key"][q["id"]]
            pts = (1 if answers[q["id"]] == k["answer"] else 0) if q["type"] == "choice" \
                else POINTS[grades[q["id"]]["grade"]]
            r = {"id": q["id"], "concept": q["concept"], "points": pts}
            if pts < 1:
                r.update(explanation=k["explanation"], hunk=k["hunk"])
                missed.append(q["concept"])
                why_ok = why_ok and kinds[q["concept"]] != "why"
            total += pts
            out.append(r)
        return {"passed": total / len(out) >= 0.8 and why_ok, "score": total, "max": len(out),
                "questions": out, "missed": missed}

    def record(self, records, exam, result, today):
        """Learning records are ticket 04's concern; grading tests ignore them."""


class Exam(Box):
    def work(self):
        from dispatcher import exams
        exams.work(self.cfg, github=self.github, run=self.session, pr_exam=Scoring(),
                   notify=self.notes.append, now=lambda: NOW, usages=self.usages)


def box_for(tmp_path, monkeypatch, spec=FIVE, **kw):
    return Exam(tmp_path, monkeypatch, session=Session(spec, **kw))


def opened(box):
    box.request()
    box.work()
    body = box.read().json()
    assert body["state"] == "open"
    return body


def answers_for(body, wrong=(), **override):
    out = {}
    for q in body["exam"]["questions"]:
        if q["type"] == "choice":
            out[q["id"]] = 0 if q["id"] in wrong else 1
        else:
            out[q["id"]] = f"ANSWER-{q['id']}"
    return {**out, **override}


def submit(box, body, answers=None, **override):
    payload = {"attempt": body["attempt"], "head_sha": body["head_sha"],
               "answers": answers if answers is not None else answers_for(body), **override}
    return box.client.post(ANSWERS, headers=HEADERS, json=payload)


def graded_once(box, body, **kw):
    assert submit(box, body, **kw).status_code == 200
    box.work()
    return box.read().json()


def result_of(body, attempt):
    [res] = [r for r in body["results"] if r["attempt"] == attempt]
    return res


def test_a_ninety_percent_submission_with_every_why_passed_posts_success_on_its_head(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = graded_once(box, opened(box))
    assert (REPO, HEAD, "exam", "success") in box.posted()
    assert body["state"] == "passed"
    res = result_of(body, body["attempt"])
    assert res["passed"] is True and (res["score"], res["max"]) == (4.5, 5)


def test_a_seventy_percent_submission_stays_pending_explains_and_opens_a_two_question_followup(
        tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC, grades={"t0": "pass", "t1": "partial"})
    first = opened(box)
    body = graded_once(box, first, answers=answers_for(first, wrong=("q0",)))
    assert not any(s[3] != "pending" for s in box.posted())
    res = result_of(body, first["attempt"])
    assert res["passed"] is False and (res["score"], res["max"]) == (3.5, 5)
    missed = {q["concept"]: q for q in res["questions"] if q["points"] < 1}
    assert set(missed) == {LEASE, SHA}
    assert missed[LEASE]["explanation"] == "EXPL-q0" and missed[LEASE]["hunk"] == "HUNK-q0"
    assert missed[SHA]["explanation"] == "EXPL-t1" and missed[SHA]["hunk"] == "HUNK-t1"
    assert body["state"] == "open"
    assert body["attempt"] == first["attempt"] + 1
    assert len(body["exam"]["questions"]) == 2
    [follow] = box.session.followups()
    assert sorted(follow["req"]["only_concepts"]) == sorted([LEASE, SHA])
    assert follow["req"]["head_sha"] == HEAD


def test_a_passed_question_carries_no_explanation_and_no_key_entry(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC)
    first = opened(box)
    response_body = graded_once(box, first, answers=answers_for(first, wrong=("q0",)))
    res = result_of(response_body, first["attempt"])
    passed = [q for q in res["questions"] if q["points"] == 1]
    assert {q["id"] for q in passed} == {"q1", "q2", "t0"}
    for q in passed:
        assert set(q) <= {"id", "concept", "points"}
    text = box.read().text
    for secret in ("EXPL-q1", "EXPL-q2", "EXPL-t0", "HUNK-q1", "HUNK-t0", "RUBRIC-"):
        assert secret not in text


def test_nothing_limits_the_attempts(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC)
    body = opened(box)
    body = graded_once(box, body, answers=answers_for(body, wrong=("q0",)))
    seen = [body["attempt"]]
    for _ in range(3):
        wrong = [q["id"] for q in body["exam"]["questions"]]
        body = graded_once(box, body, answers=answers_for(body, wrong=wrong))
        assert body["state"] == "open" and len(body["exam"]["questions"]) == 2
        seen.append(body["attempt"])
    assert seen == list(range(seen[0], seen[0] + 4))
    assert [r["attempt"] for r in body["results"]] == [seen[0] - 1, *seen[:-1]]
    assert not any(s[3] != "pending" for s in box.posted())
    assert len(box.session.followups()) == 4


def test_the_grading_session_is_new_and_gets_diff_free_text_rubrics_and_answers_only(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    generation_calls = len(box.session.calls)
    graded_once(box, body)
    [call] = box.session.graded()
    assert len(box.session.calls) == generation_calls + 1
    req = call["req"]
    assert set(req) == {"mode", "repo", "pr", "head_sha", "diff", "questions", "answers", "out"}
    assert (req["repo"], req["pr"], req["head_sha"]) == (REPO, 240, HEAD)
    files = call["files"]
    assert files["diff"] == DIFF
    questions = json.loads(files["questions"])
    assert {q["id"] for q in questions} == {"t0", "t1"}
    assert all(q["rubric"] and q["prompt"] for q in questions)
    assert "RUBRIC-PASS-t0" in files["questions"] and "PROMPT-t1" in files["questions"]
    assert json.loads(files["answers"]) == {"t0": "ANSWER-t0", "t1": "ANSWER-t1"}
    joined = json.dumps(req) + "".join(files.values())
    for foreign in ("PROMPT-q0", "EXPL-", "HUNK-", "transcript", "concepts"):
        assert foreign not in joined
    assert call["model"] in ("anthropic/claude-opus-5", "openai/gpt-5")


def test_a_multiple_choice_only_exam_is_graded_with_no_session(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=[(f"q{i}", c, "choice") for i, c in
                                               enumerate((LEASE, RETRY, SHA))])
    body = graded_once(box, opened(box))
    assert box.session.graded() == []
    assert len(box.session.calls) == 1
    assert body["state"] == "passed"
    assert (REPO, HEAD, "exam", "success") in box.posted()


def test_a_second_submission_of_the_same_attempt_is_409_and_grades_once(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert submit(box, body).status_code == 200
    assert submit(box, body).status_code == 409
    box.work()
    box.work()
    assert len(box.session.graded()) == 1
    assert submit(box, body).status_code == 409


def test_a_submission_for_an_earlier_attempt_is_409_and_starts_nothing(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch, spec=FAIL_SPEC)
    first = opened(box)
    second = graded_once(box, first, answers=answers_for(first, wrong=("q0",)))
    calls, posted = len(box.session.calls), list(box.github.statuses)
    assert submit(box, first, answers=answers_for(second)).status_code == 409
    box.work()
    assert len(box.session.calls) == calls and box.github.statuses == posted
    assert box.read().json()["attempt"] == second["attempt"]


def test_a_submission_for_another_head_sha_is_409_and_starts_nothing(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    calls = len(box.session.calls)
    assert submit(box, body, head_sha="def456").status_code == 409
    box.work()
    assert len(box.session.calls) == calls
    assert not any(s[3] == "success" for s in box.posted())
    assert box.read().json()["state"] == "open"


def test_a_submission_with_a_missing_answer_is_422_and_changes_nothing(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    calls = len(box.session.calls)
    partial = answers_for(body)
    del partial["t1"]
    assert submit(box, body, answers=partial).status_code == 422
    box.work()
    assert len(box.session.calls) == calls
    assert box.read().json()["state"] == "open"
    assert submit(box, body).status_code == 200


def test_a_result_for_a_head_that_moved_during_grading_is_dropped(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    box.session.during = lambda: setattr(box.github, "head", "def456")
    before = list(box.github.statuses)
    assert submit(box, body).status_code == 200
    box.work()
    new = box.github.statuses[len(before):]
    assert not any(s[3] != "pending" for s in new)
    assert not any(s[1] == HEAD for s in new)
    after = box.read().json()
    assert after["state"] != "passed"
    assert after["results"] == []


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("session", 600),
                                     subprocess.CalledProcessError(1, "session")])
def test_a_failed_grading_session_sets_error_keeps_the_answers_and_the_next_work_grades_again(
        tmp_path, monkeypatch, failure):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    box.session.grade_error = failure
    assert submit(box, body).status_code == 200
    box.work()
    errored = box.read().json()
    assert errored["state"] == "error" and errored["detail"]
    assert not any(s[3] == "success" for s in box.posted())
    box.session.grade_error = None
    box.work()
    assert len(box.session.graded()) == 2
    assert json.loads(box.session.graded()[1]["files"]["answers"]) == {
        "t0": "ANSWER-t0", "t1": "ANSWER-t1"}
    final = box.read().json()
    assert final["state"] == "passed"
    assert (REPO, HEAD, "exam", "success") in box.posted()
