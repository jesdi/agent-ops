"""Bypassing one question of the open attempt, through `POST|DELETE .../bypass`, `POST .../answers`,
`exams.work` and the GET, on top of test_exam_grading.py.

Interface under test (defined here, built by the ticket):
    POST   /api/exam/{owner}/{name}/{pr}/bypass   {"attempt": int, "head_sha": str, "question": id}
    DELETE /api/exam/{owner}/{name}/{pr}/bypass   {"attempt": int, "head_sha": str, "question": id}
        200 with the exam as the GET reads it. 409 when the attempt is not the open current
        attempt of that head_sha, or when another question is bypassed already (POST), or
        when `question` is not the bypassed one (DELETE). 422 for a question the exam does not
        have. A 409 or 422 changes nothing.
    GET .../ `bypassed`: the open attempt's bypassed question id, or None.
    POST .../answers accepts a bypassed question without an answer.
    pr_exam.score(exam: Path, answers: Path, grades: Path, flags: Path | None = None) -> dict
        `flags` holds {"bypassed": [id], "assisted": [id]}, written once at submit; what
        `pr_exam.py score --flags` takes. A bypassed question comes out as
        {"id", "concept", "help": "bypassed", "explanation", "hunk"}: no points, and it
        counts in neither `score` nor `max`.
"""
import json
from pathlib import Path

from tests.test_exam_generation import GET, HEADERS
from tests.test_exam_grading import (
    Exam, POINTS, Scoring, Session, answers_for, graded_once, opened, result_of, submit)

BYPASS = f"{GET}/bypass"
LEASE, RETRY, SHA, WHY, RUBRIC = "lease-ownership", "retry-budget", "base-sha-recheck", "why-lease", "rubric-fixture"
SIX = [("q1", LEASE, "choice"), ("q2", RETRY, "choice"), ("q3", SHA, "choice"),
       ("q4", WHY, "text"), ("q5", RUBRIC, "text"), ("q6", "x", "text")]
GRADES = {"q4": "pass", "q5": "pass", "q6": "pass"}


class Flagging(Scoring):
    """Scores like `pr_exam.py score --flags`, and keeps the flags file it was given."""
    def __init__(self):
        self.seen = []

    def score(self, exam, answers, grades, flags=None):
        flag_doc = json.loads(Path(flags).read_text()) if flags else {}
        self.seen.append(flag_doc)
        exam, answers, grades = (json.loads(Path(p).read_text()) for p in (exam, answers, grades))
        skipped = set(flag_doc.get("bypassed", []))
        out, total, count = [], 0, 0
        for q in exam["questions"]:
            k = exam["key"][q["id"]]
            if q["id"] in skipped:
                out.append({"id": q["id"], "concept": q["concept"], "help": "bypassed",
                            "explanation": k["explanation"], "hunk": k["hunk"]})
                continue
            pts = (1 if answers[q["id"]] == k["answer"] else 0) if q["type"] == "choice" \
                else POINTS[grades[q["id"]]["grade"]]
            r = {"id": q["id"], "concept": q["concept"], "points": pts}
            if pts < 1:
                r.update(explanation=k["explanation"], hunk=k["hunk"])
            total, count = total + pts, count + 1
            out.append(r)
        return {"passed": total / count >= 0.8, "score": total, "max": count, "questions": out,
                "missed": [r["concept"] for r in out if r.get("points", 1) < 1]}


class Box(Exam):
    PR_EXAM = Flagging


def box_for(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch, session=Session(SIX, grades=GRADES))


def payload(body, question, **override):
    return {"attempt": body["attempt"], "head_sha": body["head_sha"], "question": question, **override}


def bypass(box, body, question, **override):
    return box.client.post(BYPASS, headers=HEADERS, json=payload(body, question, **override))


def unbypass(box, body, question, **override):
    return box.client.request("DELETE", BYPASS, headers=HEADERS, json=payload(body, question, **override))


def without(answers, *ids):
    return {k: v for k, v in answers.items() if k not in ids}


def test_a_second_bypass_is_refused_until_the_first_is_undone(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert body["bypassed"] is None
    first = bypass(box, body, "q6")
    assert first.status_code == 200
    assert first.json() == box.read().json() and first.json()["bypassed"] == "q6"
    assert bypass(box, body, "q2").status_code == 409
    assert box.read().json()["bypassed"] == "q6"
    undone = unbypass(box, body, "q6")
    assert undone.status_code == 200 and undone.json()["bypassed"] is None
    assert bypass(box, body, "q2").status_code == 200
    assert box.read().json()["bypassed"] == "q2"


def test_a_bypass_of_a_question_the_exam_lacks_is_422_and_changes_nothing(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert bypass(box, body, "q9").status_code == 422
    assert box.read().json()["bypassed"] is None


def test_submit_accepts_the_bypassed_question_unanswered_and_grades(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert bypass(box, body, "q6").status_code == 200
    assert submit(box, body, answers=without(answers_for(body), "q6")).status_code == 202
    assert box.read().json()["state"] == "grading"


def test_submit_is_refused_for_an_unanswered_question_that_is_not_bypassed(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert submit(box, body, answers=without(answers_for(body), "q6")).status_code == 422
    assert box.read().json()["state"] == "open"
    assert bypass(box, body, "q5").status_code == 200
    assert submit(box, body, answers=without(answers_for(body), "q6")).status_code == 422
    assert box.read().json()["state"] == "open"


def test_a_bypass_is_refused_while_grading(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert submit(box, body).status_code == 202
    assert bypass(box, body, "q6").status_code == 409
    box.work()
    assert box.pr_exam.seen == [{"bypassed": [], "assisted": []}]


def test_a_bypass_is_refused_once_the_attempt_passed(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert graded_once(box, body)["state"] == "passed"
    assert bypass(box, body, "q6").status_code == 409
    assert unbypass(box, body, "q6").status_code == 409


def test_a_bypass_is_refused_for_an_earlier_attempt_and_for_another_head(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    first = opened(box)
    second = graded_once(box, first, answers=answers_for(first, wrong=("q1", "q2")))
    assert second["state"] == "open" and second["attempt"] == first["attempt"] + 1
    qid = second["exam"]["questions"][0]["id"]
    assert bypass(box, first, qid).status_code == 409
    assert bypass(box, second, qid, head_sha="def456").status_code == 409
    assert box.read().json()["bypassed"] is None
    assert bypass(box, second, qid).status_code == 200


def test_a_bypassed_question_shows_in_the_result_outside_the_points(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert bypass(box, body, "q6").status_code == 200
    final = graded_once(box, body, answers=without(answers_for(body), "q6"))
    assert final["state"] == "passed"
    res = result_of(final, body["attempt"])
    assert res["passed"] is True and (res["score"], res["max"]) == (5, 5)
    [q6] = [q for q in res["questions"] if q["id"] == "q6"]
    assert q6["help"] == "bypassed" and "points" not in q6
    assert (q6["explanation"], q6["hunk"]) == ("EXPL-q6", "HUNK-q6")


def test_an_answer_left_in_the_draft_of_a_bypassed_question_is_harmless(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert bypass(box, body, "q6").status_code == 200
    final = graded_once(box, body)
    res = result_of(final, body["attempt"])
    assert (res["score"], res["max"]) == (5, 5)
    assert [q["help"] for q in res["questions"] if q.get("help")] == ["bypassed"]


def test_grading_scores_with_the_flags_written_at_submit(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert bypass(box, body, "q6").status_code == 200
    assert unbypass(box, body, "q6").status_code == 200
    assert bypass(box, body, "q2").status_code == 200
    graded_once(box, body, answers=without(answers_for(body), "q2"))
    assert box.pr_exam.seen == [{"bypassed": ["q2"], "assisted": []}]


def watch_writes(monkeypatch, state_dir):
    """Record, for each atomic write of exams.py, its file name and whether `exams.lock` was held."""
    from dispatcher import exams
    from dispatcher.state import state_lock
    real, writes = exams.write_json_atomic, []

    def spy(path, data, *a, **kw):
        try:
            with state_lock(state_dir, exams.LOCK, timeout=0):
                held = False
        except TimeoutError:
            held = True
        writes.append((Path(path).name, held))
        return real(path, data, *a, **kw)
    monkeypatch.setattr(exams, "write_json_atomic", spy)
    return writes


def test_the_flags_are_written_once_under_the_exams_lock_with_the_state_change(tmp_path, monkeypatch):
    box = box_for(tmp_path, monkeypatch)
    body = opened(box)
    assert bypass(box, body, "q6").status_code == 200
    writes = watch_writes(monkeypatch, box.cfg.state_dir)
    answers = without(answers_for(body), "q6")
    assert submit(box, body, answers=answers).status_code == 202
    assert submit(box, body, answers=answers).status_code == 409
    flags = [w for w in writes if w[0] == "flags.json"]
    assert flags == [("flags.json", True)]
    assert all(held for _, held in writes)
    assert [n for n, _ in writes].index("flags.json") < len(writes) - 1   # state change follows, same lock
    [flag_file] = Path(box.cfg.state_dir).glob("exams/**/flags.json")
    assert json.loads(flag_file.read_text()) == {"bypassed": ["q6"], "assisted": []}
    assert box.read().json()["state"] == "grading"
    box.work()
    assert box.pr_exam.seen == [{"bypassed": ["q6"], "assisted": []}]
