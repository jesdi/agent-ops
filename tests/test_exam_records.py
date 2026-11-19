"""Learning records through `exams.work` with injected fakes: what the host hands `pr_exam`.

Interface under test (defined here, built by the ticket), on top of test_exam_grading.py:
    pr_exam.record(records: Path, exam: Path, result: Path, today: str) -> None
        what `pr_exam.py record --records --exam --result --today` does: `result` is the dict
        `score` returned for the attempt, `records` is `<state_dir>/exam-records/<owner>/<name>`.
        Raises on a nonzero exit.
    The host calls it once per graded attempt, with the exam's key file and the UTC date.
    The generation request's `records` is that same directory, listing every record written.
"""
import json
import re
from pathlib import Path

from tests.test_exam_generation import HEAD, HEADERS, REPO
from tests.test_exam_grading import (FAIL_SPEC, FIVE, LEASE, SHA, Exam, Scoring, Session,
                                     answers_for, make_exam, opened)

OTHER = "jesdi/agent-ops"


class Recording(Scoring):
    """Writes records the way the skill's script does: numbered after the highest one."""

    def __init__(self):
        self.calls = []

    def record(self, records, exam, result, today):
        records = Path(records)
        exam_doc, res = json.loads(Path(exam).read_text()), json.loads(Path(result).read_text())
        self.calls.append({"records": records, "today": today, "result": res,
                           "exam": exam_doc, "result_questions": res["questions"]})
        records.mkdir(parents=True, exist_ok=True)
        n = max([int(p.name[:4]) for p in records.glob("*.md")], default=0)
        for q in res["questions"]:
            n += 1
            state = "demonstrated" if q["points"] == 1 else "misconception"  # partial counts as missed
            (records / f"{n:04d}-{q['concept']}.md").write_text(
                f"---\nconcept: {q['concept']}\nstate: {state}\ndate: {today}\n"
                f"source: {exam_doc['repo']}#{exam_doc['pr']}\nstatus: active\n---\n")


class RecSession(Session):
    """Also lists the records directory each generation was handed."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.listings = []

    def __call__(self, request, *, model, timeout):
        req = json.loads(Path(request).read_text())
        if req["mode"] != "grade":
            self.listings.append((req["records"], req["pr"],
                                  sorted(p.name for p in Path(req["records"]).iterdir())))
        super().__call__(request, model=model, timeout=timeout)
        if req["mode"] != "grade":   # the exam names the PR it was generated for
            out = Path(req["out"])
            out.write_text(json.dumps({**json.loads(out.read_text()),
                                       "repo": req["repo"], "pr": req["pr"]}))


class Box(Exam):
    PR_EXAM = Recording

    def __init__(self, tmp_path, monkeypatch, spec=FIVE, **kw):
        super().__init__(tmp_path, monkeypatch, session=RecSession(spec, **kw))

    def api(self, repo, pr):
        return f"/api/exam/{repo}/{pr}"

    def open(self, repo, pr):
        r = self.client.post("/api/exam", headers=HEADERS, json={"repo": repo, "pr": pr})
        assert r.status_code == 200
        self.work()
        body = self.client.get(self.api(repo, pr), headers=HEADERS).json()
        assert body["state"] == "open"
        return body

    def grade(self, repo, pr, body, **kw):
        r = self.client.post(self.api(repo, pr) + "/answers", headers=HEADERS, json={
            "attempt": body["attempt"], "head_sha": body["head_sha"],
            "answers": answers_for(body, **kw)})
        assert r.status_code == 200

    def records(self, repo):
        d = Path(self.cfg.state_dir) / "exam-records" / repo
        return sorted(p.name for p in d.glob("*.md")) if d.is_dir() else []


def fields(box, repo, name):
    text = (Path(box.cfg.state_dir) / "exam-records" / repo / name).read_text()
    return dict(re.findall(r"^(\w+): *(.*)$", text, re.M))


PASS_ALL = {"t0": "pass", "t1": "pass"}
PARTIAL = {"t0": "pass", "t1": "partial"}   # the 70% scenario: lease wrong, base SHA partial


def test_a_passing_submission_writes_one_demonstrated_record_per_concept(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch, grades=PASS_ALL)
    body = box.open(REPO, 240)
    box.grade(REPO, 240, body)
    box.work()
    names = box.records(REPO)
    assert sorted(n[5:-3] for n in names) == sorted(c["id"] for c in make_exam(HEAD, FIVE)["concepts"])
    assert {fields(box, REPO, n)["state"] for n in names} == {"demonstrated"}
    [call] = box.pr_exam.calls
    assert call["records"] == Path(box.cfg.state_dir) / "exam-records" / REPO
    assert call["today"] == "2026-11-19"
    assert call["exam"]["head_sha"] == HEAD and "key" in call["exam"]


def test_a_failing_submission_that_misses_two_concepts_writes_two_misconception_records(
        tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch, spec=FAIL_SPEC, grades=PARTIAL)
    body = box.open(REPO, 240)
    box.grade(REPO, 240, body, wrong=("q0",))
    box.work()
    states = {n[5:-3]: fields(box, REPO, n)["state"] for n in box.records(REPO)}
    assert sorted(c for c, s in states.items() if s == "misconception") == sorted([LEASE, SHA])
    assert list(states.values()).count("misconception") == 2


def test_a_pr_of_another_repo_writes_only_under_its_own_directory(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch, grades=PASS_ALL)
    body = box.open(OTHER, 7)
    box.grade(OTHER, 7, body)
    box.work()
    assert len(box.records(OTHER)) == 5
    assert box.records(REPO) == []
    assert box.pr_exam.calls[0]["records"] == Path(box.cfg.state_dir) / "exam-records" / OTHER


def test_the_first_generation_gets_an_empty_directory_and_the_next_one_gets_the_records(
        tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch, grades=PASS_ALL)
    body = box.open(REPO, 240)
    box.grade(REPO, 240, body)
    box.work()
    written = box.records(REPO)
    assert written
    box.open(REPO, 241)
    first, second = box.session.listings
    assert first[2] == []
    assert second[0] == first[0] and second[2] == written


def test_gradings_of_two_prs_of_one_repo_in_one_work_keep_all_their_records(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch, grades=PASS_ALL)
    a, b = box.open(REPO, 240), box.open(REPO, 241)
    box.grade(REPO, 240, a)
    box.grade(REPO, 241, b)
    box.work()
    names = box.records(REPO)
    assert len(names) == 10
    assert len({n[:4] for n in names}) == 10
    sources = [fields(box, REPO, n)["source"] for n in names]
    assert sorted(set(sources)) == [f"{REPO}#240", f"{REPO}#241"]
    assert sources.count(f"{REPO}#240") == 5


def test_a_grading_result_dropped_because_the_head_moved_writes_no_record(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch, grades=PASS_ALL)
    a, b = box.open(REPO, 240), box.open(REPO, 241)
    moves = iter(["def456", HEAD])   # the head moves during the first grading only
    box.session.during = lambda: setattr(box.github, "head", next(moves))
    box.grade(REPO, 240, a)
    box.grade(REPO, 241, b)
    box.work()
    names = box.records(REPO)
    assert len(names) == 5
    assert len({fields(box, REPO, n)["source"] for n in names}) == 1
    assert len(box.pr_exam.calls) == 1
