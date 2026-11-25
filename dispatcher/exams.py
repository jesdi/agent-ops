"""Exam state on the box, `<state_dir>/exams/<owner>/<name>/<pr>/state.json`, and the
jobs that move it.

Every write holds the exams lock and renames atomically. All exam work is one job
at a time per exam, `job = {kind, head_sha, attempt, started}`: claimed under the
lock, run outside it, and applied under the lock only while it is still the
recorded job. The kind selects the operation: `generate` (first exam), `delta`
(judgment after a pass), `followup` (missed concepts after a fail), `grade`.

What the PR makes of the exam (its head, a merge or close, a bypass, debt, the `exam`
status) is `exam_lifecycle`'s: a request, the daily sweep and the end of every job bring
the exam in line with the PR through `_reconcile`."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from enum import StrEnum
from types import SimpleNamespace
from typing import Iterator

from dispatcher import exam_lifecycle, exam_migration, exam_threads, priority
from dispatcher.config import routed_providers
from dispatcher.exam_lifecycle import ExamState
from dispatcher.exam_threads import Ask, Prompt
from dispatcher.models import triage_entry
from dispatcher.state import state_lock, write_json_atomic
from dispatcher.usage import admits

LOCK = "exams.lock"
TIMEOUT = 1200   # a session runs 20 minutes at most
APP = "exam-app.json"
STAGED = ("next-exam.json", "next-public.json")   # a generated exam before it is applied
CHATTING = (ExamState.GENERATING, ExamState.OPEN, ExamState.GRADING, ExamState.PASSED)


class Refusal(StrEnum):
    """Why the exam refused an operator's change; the console maps each to its HTTP status."""
    CONFLICT = "conflict"       # not what the exam's state allows now
    UNKNOWN = "unknown"         # no such question
    INCOMPLETE = "incomplete"   # an answer is missing
    BUSY = "busy"               # a turn already runs there
    USAGE = "usage"             # the usage gate admits no model
    TOO_LONG = "too long"       # the turn's prompt is over the skill's limit
    UNAVAILABLE = "unavailable" # the skill could not build the turn's prompt


class Refused(Exception):
    """An operator's change the exam refused; it changed nothing."""

    def __init__(self, refusal: Refusal, detail: str):
        super().__init__(detail)
        self.refusal, self.detail = refusal, detail


def _path(state_dir: str | Path, repo: str, pr: int) -> Path:
    return Path(state_dir) / "exams" / repo / str(pr) / exam_lifecycle.STATE_FILE


def _dir(state_dir, repo: str, pr: int) -> Path:
    return _path(state_dir, repo, pr).parent


def attempt_dir(state_dir, repo: str, pr: int, n: int) -> Path:
    """Where attempt `n` keeps its exam, answers, grades and result."""
    return _dir(state_dir, repo, pr) / "attempts" / str(n)


def read(state_dir: str | Path, repo: str, pr: int) -> dict | None:
    """The exam's state, its legacy files migrated; takes the exams lock."""
    with state_lock(state_dir, LOCK):
        return _load(state_dir, repo, pr)


def _load(state_dir: str | Path, repo: str, pr: int) -> dict | None:
    """`read` for a caller that holds the exams lock."""
    try:
        doc = json.loads(_path(state_dir, repo, pr).read_text())
    except FileNotFoundError:
        return None
    exam_migration.migrate(_dir(state_dir, repo, pr), doc, lambda: _write(state_dir, doc), _line)
    return doc


def _write(state_dir, doc: dict) -> None:
    write_json_atomic(_path(state_dir, doc["repo"], doc["pr"]), doc, indent=2)


def request(state_dir: str | Path, repo: str, pr: int) -> dict:
    """Record that this PR's exam is wanted; a repeat finds the same state. Each request
    raises `requested`; work reconciles with GitHub until `reconciled` catches up. `stale`
    asks for the same from a job's claim on: a reconcile that fails after the job's result
    was applied is made again by the next work."""
    with state_lock(state_dir, LOCK):
        doc = _load(state_dir, repo, pr)
        if doc is None:
            doc = {"repo": repo, "pr": pr, "title": "", "state": ExamState.NEW, "head_sha": None,
                   "debt": False, "detail": "", "attempt": 0, "requested": 0, "reconciled": 0,
                   "stale": False, "job": None, "status": None, "due": None, "merged": False,
                   "passed_concepts": [], "grading": None, "followup": None, "delta": False,
                   "results": [], "bypassed": None}
        elif doc["state"] == ExamState.ERROR and not doc["grading"]:
            doc["state"] = ExamState.NEW   # a request after a failed job retries it
        doc["requested"] += 1
        _write(state_dir, doc)
        return doc


def public(state_dir: str | Path, repo: str, pr: int, attempt: int) -> dict:
    """The served questions of `attempt`, as pr_exam.public projected them; never the exam file."""
    return json.loads((attempt_dir(state_dir, repo, pr, attempt) / "public.json").read_text())


def view(state_dir: str | Path, repo: str, pr: int) -> dict | None:
    """The exam as its page reads it, the state and every attempt file read under one lock:
    `exam` is the open attempt, `attempts` every submitted attempt of this head, oldest first,
    with its result once graded. Each attempt carries its served questions with their chat,
    and what it takes now: `chat` (thread and general turns), `bypass`, and per question the
    tips left (None when it takes none). Neither holds a key."""
    with state_lock(state_dir, LOCK):
        doc = _load(state_dir, repo, pr)
        if doc is None:
            return None
        return {**doc, "tip_cap": exam_threads.TIPS,
                "exam": _attempt(state_dir, doc, doc["attempt"]) if doc["state"] == ExamState.OPEN else None,
                "attempts": [{"attempt": n, **_attempt(state_dir, doc, n), "result": result}
                             for n, result in _submitted(doc)]}


def _submitted(doc: dict) -> list[tuple[int, dict | None]]:
    """This head's submitted attempts, oldest first, each with its result: the graded ones,
    then the one grading (no result yet)."""
    grading = [(doc["grading"]["attempt"], None)] if doc["grading"] else []
    return [(r["attempt"], r) for r in doc["results"]] + grading


def _attempt(state_dir, doc: dict, n: int) -> dict:
    """Attempt `n`'s served questions with their chat, its general chat and what it takes now."""
    adir = attempt_dir(state_dir, doc["repo"], doc["pr"], n)
    served, turns, tips = public(state_dir, doc["repo"], doc["pr"], n), exam_threads.turns(adir), _taking(doc, n)
    return {**served, "chat": _chatting(adir, doc, n), "bypass": tips, "general": exam_threads.general_view(turns),
            "questions": [{**q, **exam_threads.question_view(q["id"], turns, tips)} for q in served["questions"]]}


def _taking(doc: dict, n: int) -> bool:
    """Attempt `n` is the open one, taking answers, tips and a bypass."""
    return doc["state"] == ExamState.OPEN and doc["attempt"] == n


def _chatting(adir: Path, doc: dict, n: int) -> bool:
    """Attempt `n` takes question and general turns: it is one of this head's attempts (open,
    grading or graded) while the exam is in a state that chats. A turn is built from the
    attempt's exam, so one whose exam was not kept takes none."""
    kept = [m for m, _ in _submitted(doc)] + ([doc["attempt"]] if _taking(doc, doc["attempt"]) else [])
    return doc["state"] in CHATTING and n in kept and (adir / "exam.json").is_file()


def app(state_dir: str | Path) -> dict | None:
    try:
        return json.loads((Path(state_dir) / APP).read_text())
    except FileNotFoundError:
        return None


def _exams(state_dir: str | Path) -> Iterator[dict]:
    """Every exam on the box, in path order."""
    for path in sorted(Path(state_dir, "exams").glob(f"*/*/*/{exam_lifecycle.STATE_FILE}")):
        try:
            yield json.loads(path.read_text())
        except FileNotFoundError:   # deleted since the listing
            continue


def pending(state_dir: str | Path) -> bool:
    """True when some exam has work for the runner."""
    return any(_kind(doc) or _wanted(doc) or _due(doc) for doc in _exams(state_dir))


def listing(state_dir: str | Path) -> list[dict]:
    """Every exam on the box, as stored; the console's debt list is those with `debt`."""
    return list(_exams(state_dir))


def _update(state_dir, repo: str, pr: int, **fields) -> dict:
    with state_lock(state_dir, LOCK):
        doc = _load(state_dir, repo, pr)
        doc.update(fields)
        _write(state_dir, doc)
        return doc


def _wanted(doc: dict) -> bool:
    """A request, or a job's result, still waits for a reconcile with GitHub."""
    return doc["requested"] > doc["reconciled"] or doc["stale"]


def _due(doc: dict) -> dict | None:
    """The status still to post, None when it is posted."""
    return doc["due"] if doc["due"] and doc["due"] != doc["status"] else None


def _kind(doc: dict) -> str | None:
    """The job this exam's state asks for; None when it waits for the operator or a request."""
    if doc["grading"]:
        return "grade"
    if doc["state"] not in (ExamState.NEW, ExamState.GENERATING):
        return None
    return "delta" if doc["delta"] else "followup" if doc["followup"] else "generate"


def _live(job: dict | None, now) -> bool:
    return bool(job) and now() - datetime.fromisoformat(job["started"]) < timedelta(seconds=TIMEOUT + 300)


FIXED = ("grade", "followup")   # jobs on the answers or missed concepts of the current attempt


def _claimable(cur: dict, job: dict, now) -> bool:
    """`job["attempt"]` is the attempt the job works on, so it must still be the exam's."""
    if _kind(cur) != job["kind"] or _live(cur["job"], now) or cur["attempt"] != job["attempt"]:
        return False
    return job["kind"] not in FIXED or cur["head_sha"] == job["head_sha"]


def _claim(cfg, doc: dict, job: dict, now) -> bool:
    """Record `job` unless the exam moved on since `doc` was read or a live job is recorded."""
    with state_lock(cfg.state_dir, LOCK):
        cur = _load(cfg.state_dir, doc["repo"], doc["pr"])
        if cur is None or not _claimable(cur, job, now):
            return False
        cur.update(job=job, detail="", stale=True,
                   state=ExamState.GRADING if job["kind"] == "grade" else ExamState.GENERATING)
        _write(cfg.state_dir, cur)
        return True


def _finish(cfg, doc: dict, job: dict, change) -> dict | None:
    """End `job` with `change(cur, d)` under the lock, only while it is still the exam's job;
    the exam after it, None when the exam had moved on."""
    with state_lock(cfg.state_dir, LOCK):
        cur = _load(cfg.state_dir, doc["repo"], doc["pr"])
        if cur is None or cur["job"] != job:
            return None
        cur["job"] = None
        change(cur, _dir(cfg.state_dir, doc["repo"], doc["pr"]))
        _write(cfg.state_dir, cur)
        return cur


def _job_dir(d: Path) -> Path:
    """A fresh job dir: the only directory the session can write."""
    job = d / "job"
    shutil.rmtree(job, ignore_errors=True)
    job.mkdir(parents=True)
    return job


def _run(io, request: Path, model: str, what: str) -> str:
    """Run one session; "" when it ran, else why it failed."""
    try:
        io.run(request, model=model, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"{what} ran past {TIMEOUT // 60} minutes"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"{what} failed: {exc}"
    return ""


def _inputs(io, pr, repo: str, n: int, job: Path) -> tuple[str, str | None]:
    """Write the session's diff, body and glossary; (the diff, the glossary's path or None)."""
    glossary = io.github.file(repo, pr["base_ref"], "GLOSSARY.md")
    diff = io.github.diff(repo, n)
    (job / "diff.patch").write_text(diff)
    (job / "body.md").write_text(pr["body"] or "")
    if glossary is None:
        return diff, None
    (job / "GLOSSARY.md").write_text(glossary)
    return diff, str(job / "GLOSSARY.md")


def _prior(prior: dict | None) -> dict:
    prior = prior or {}
    return {"prior_concepts": [c["id"] for c in prior.get("concepts", [])],
            "prior_prompts": [q["prompt"] for q in prior.get("questions", [])]}


def _spec(cfg, doc, kind: str, sha: str, job: Path, glossary, now, prior: dict | None) -> dict:
    """The generation request; a delta judgment also gets the passed diff and concepts."""
    spec = {
        "mode": {"generate": "first"}.get(kind, kind), "repo": doc["repo"], "pr": doc["pr"],
        "head_sha": sha, "diff": str(job / "diff.patch"), "body": str(job / "body.md"),
        "glossary": glossary, "records": str(Path(cfg.state_dir) / "exam-records" / doc["repo"]),
        "today": now().astimezone(timezone.utc).date().isoformat(),
        "only_concepts": (doc["followup"] or {}).get("only"), "out": str(job / "exam.json"),
        **_prior(prior)}
    if kind == "delta":   # pr_exam refuses a delta that lists a concept already passed
        shutil.copyfile(job.parent / "passed.diff", job / "passed.diff")
        spec.update(passed_diff=str(job / "passed.diff"), passed_concepts=doc["passed_concepts"],
                    prior_concepts=doc["passed_concepts"])
    return spec


def _exam_error(io, job: Path, spec: dict, model: str) -> str:
    """Run the generation session and validate its exam; "" when it is valid."""
    out, request = job / "exam.json", job / "request.json"
    request.write_text(json.dumps(spec))
    if error := _run(io, request, model, "the session"):
        return error
    if not out.exists():
        return "the session wrote no exam file"
    if out.is_symlink() or not out.is_file():   # the job dir is writable by the session
        return "the session's exam file is not a regular file"
    request.unlink(missing_ok=True)   # a symlink here must not take our write through it
    request.write_text(json.dumps(spec))   # validate against our request, not a rewritten one
    return "; ".join(io.pr_exam.validate(out, request))


def _generate(cfg, doc, job: dict, pr, model, io) -> tuple[str, str, str]:
    """Run one generation session and stage its exam out of the session's reach;
    (outcome, detail, diff). The outcome is "open", "error", or "passed" for a
    delta that found no new concept."""
    repo, n = doc["repo"], doc["pr"]
    d = _dir(cfg.state_dir, repo, n)
    jd = _job_dir(d)
    (Path(cfg.state_dir) / "exam-records" / repo).mkdir(parents=True, exist_ok=True)
    diff, glossary = _inputs(io, pr, repo, n, jd)
    prior = (json.loads((attempt_dir(cfg.state_dir, repo, n, job["attempt"]) / "exam.json").read_text())
             if job["kind"] == "followup" else None)
    spec = _spec(cfg, doc, job["kind"], job["head_sha"], jd, glossary, io.now, prior)
    if error := _exam_error(io, jd, spec, model):
        return "error", error, diff
    out = jd / "exam.json"
    if job["kind"] == "delta" and not json.loads(out.read_text())["questions"]:
        return "passed", "", diff
    write_json_atomic(d / STAGED[1], io.pr_exam.public(out))
    shutil.copyfile(out, d / STAGED[0])
    return "open", "", diff


def entry(cfg, usages, when):
    """The first `models.triage` entry the usage gate admits, None when none is: the exam
    runner's gate, which the console's chat turns share."""
    return triage_entry(
        cfg.models, lambda m: admits(usages, m, when, cfg.pace).admitted,
        priority.order(priority.load(cfg.state_dir, routed_providers(cfg)),
                       usages, when, cfg.pace))


def _is_open(doc: dict | None, attempt: int, head_sha: str) -> bool:
    return doc is not None and doc["head_sha"] == head_sha and _taking(doc, attempt)


def _answers(q: dict, a) -> bool:
    """A choice is answered by an int, a text question by a string."""
    return not isinstance(a, bool) and isinstance(a, (int, str)) and isinstance(a, int) == (q["type"] == "choice")


def submit(state_dir, repo: str, pr: int, attempt: int, head_sha: str, answers: dict) -> None:
    """Accept the answers of the open attempt and seal its flags. Refused (changing nothing)
    with CONFLICT when it is repeated, or for an earlier attempt or another head, and with
    INCOMPLETE when a question other than the bypassed one has no answer."""
    with state_lock(state_dir, LOCK):
        doc = _load(state_dir, repo, pr)
        if not _is_open(doc, attempt, head_sha):
            raise Refused(Refusal.CONFLICT, "this attempt is not open for answers")
        questions = public(state_dir, repo, pr, attempt)["questions"]
        if not _complete(questions, answers, doc["bypassed"]):
            raise Refused(Refusal.INCOMPLETE, "every question needs an answer")
        d = attempt_dir(state_dir, repo, pr, attempt)
        _save_submission(d, questions, answers, doc["bypassed"], exam_threads.assisted(exam_threads.turns(d)))
        doc.update(state=ExamState.GRADING, grading={"attempt": attempt}, bypassed=None)
        _write(state_dir, doc)


def _complete(questions: list, answers: dict, skipped: str | None) -> bool:
    """Every question but the bypassed one has a well-typed answer."""
    return all(q["id"] == skipped or _answers(q, answers.get(q["id"])) for q in questions)


def _save_submission(attempt_dir: Path, questions: list, answers: dict, skipped: str | None,
                     assisted: list[str]) -> None:
    """Write the submitted answers and the attempt's flags snapshot."""
    write_json_atomic(attempt_dir / "answers.json",
                      {q["id"]: answers[q["id"]] for q in questions if q["id"] in answers})
    write_json_atomic(attempt_dir / "flags.json", {"bypassed": [skipped] if skipped else [], "assisted": assisted})


def bypass(state_dir, repo: str, pr: int, attempt: int, head_sha: str, question: str, on: bool) -> None:
    """Bypass `question` of the open attempt, or undo it. Refused with CONFLICT for any but the
    open current attempt, a second bypass, or undoing another question, and with UNKNOWN for a
    question the attempt lacks."""
    with state_lock(state_dir, LOCK):
        doc = _load(state_dir, repo, pr)
        if not _is_open(doc, attempt, head_sha):
            raise Refused(Refusal.CONFLICT, "this attempt cannot change its bypass")
        _require(question, public(state_dir, repo, pr, attempt)["questions"])
        if doc["bypassed"] != (None if on else question):   # a bypass needs none held, an undo this one
            raise Refused(Refusal.CONFLICT, "this attempt cannot change its bypass")
        doc["bypassed"] = question if on else None
        _write(state_dir, doc)


def _require(question: str, questions: list[dict]) -> None:
    if question not in {q["id"] for q in questions}:
        raise Refused(Refusal.UNKNOWN, "the exam has no such question")


def _grading_request(job: Path, doc, sha: str, diff: str, key: dict, answers: dict, texts: list) -> Path:
    """Write the grading session's inputs; the request's path."""
    (job / "diff.patch").write_text(diff)
    (job / "questions.json").write_text(json.dumps([
        {"id": q["id"], "prompt": q["prompt"], "rubric": key["key"][q["id"]]["rubric"]} for q in texts]))
    (job / "answers.json").write_text(json.dumps({q["id"]: answers[q["id"]] for q in texts}))
    request = job / "request.json"
    request.write_text(json.dumps({
        "mode": "grade", "repo": doc["repo"], "pr": doc["pr"], "head_sha": sha,
        "diff": str(job / "diff.patch"), "questions": str(job / "questions.json"),
        "answers": str(job / "answers.json"), "out": str(job / "grades.json")}))
    return request


def _grades(out: Path, texts: list) -> tuple[dict | None, str]:
    """The grading session's grades of `texts`, normalized to {id: {"grade", "feedback"}}
    with the feedback a capped line or ""; (None, error) unless every grade is well formed.
    The session's file is untrusted: nothing else of it goes further."""
    if out.is_symlink() or not out.is_file():
        return None, "the grading session wrote no regular grades file"
    try:
        grades = _normalized(json.loads(out.read_text()), texts)
        if all(g["grade"] in ("pass", "partial", "fail") for g in grades.values()):
            return grades, ""
    except (ValueError, KeyError, TypeError):
        pass
    return None, "the grading session's grades are malformed"


def _normalized(raw, texts: list) -> dict:
    """{id: {"grade", "feedback"}} of `texts` from the session's grades; raises KeyError or
    TypeError when a grade is missing or not an object."""
    return {q["id"]: {"grade": raw[q["id"]]["grade"], "feedback": _line(raw[q["id"]].get("feedback"))}
            for q in texts}


def _line(feedback) -> str:
    """The grader's feedback on one question, capped; "" when it is not text."""
    return feedback[:1000] if isinstance(feedback, str) else ""


def _texts(attempt_dir: Path, key: dict) -> list:
    """The free-text questions of the attempt left to grade: none the attempt bypassed."""
    skipped = json.loads((attempt_dir / "flags.json").read_text())["bypassed"]
    return [q for q in key["questions"] if q["type"] == "text" and q["id"] not in skipped]


def _grade(cfg, doc, attempt_dir: Path, sha, diff: str, model, io) -> tuple[dict | None, str]:
    """Grades of the free-text answers in `attempt_dir` by a fresh session; (grades, error)."""
    key = json.loads((attempt_dir / "exam.json").read_text())
    texts = _texts(attempt_dir, key)
    if not texts:
        return {}, ""
    answers = json.loads((attempt_dir / "answers.json").read_text())
    job = _job_dir(_dir(cfg.state_dir, doc["repo"], doc["pr"]))
    request = _grading_request(job, doc, sha, diff, key, answers, texts)
    if error := _run(io, request, model, "the grading session"):
        return None, error
    return _grades(job / "grades.json", texts)


def _score(attempt_dir: Path, grades: dict, pr_exam) -> tuple[dict | None, str]:
    """Score the graded attempt into result.json; (scored, error)."""
    (attempt_dir / "grades.json").write_text(json.dumps(grades))
    try:
        scored = pr_exam.score(attempt_dir / "exam.json", attempt_dir / "answers.json",
                               attempt_dir / "grades.json", attempt_dir / "flags.json")
    except (ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        return None, f"scoring failed: {exc}"
    for q in scored["questions"]:   # the grader's line rides along with each free-text question
        if q["id"] in grades:
            q["feedback"] = grades[q["id"]]["feedback"]
    (attempt_dir / "result.json").write_text(json.dumps(scored))
    return scored, ""


def _result(doc, scored: dict) -> dict:
    """The attempt as the page shows it: explanations only where points were lost."""
    return {"attempt": doc["attempt"], "passed": scored["passed"], "score": scored["score"],
            "max": scored["max"], "questions": [_shown(q) for q in scored["questions"]]}


def _shown(q: dict) -> dict:
    """One scored question as the page shows it: the explanation, hunk and feedback only where
    points were lost or the question was bypassed."""
    keep = ("id", "concept", "points", "help")
    if q.get("help") == "bypassed" or q["points"] < 1:
        keep += ("explanation", "hunk", "feedback")
    return {k: v for k, v in q.items() if k in keep}


def _graded_fields(doc, scored: dict, exam: dict) -> dict:
    missed = set() if scored["passed"] else set(scored["missed"])   # missed concepts return in the follow-up
    return {"grading": None, "results": [*doc["results"], _result(doc, scored)],
            "passed_concepts": list(dict.fromkeys(
                [*doc["passed_concepts"], *(c["id"] for c in exam["concepts"] if c["id"] not in missed)]))}


def _record(cfg, attempt_dir: Path, repo: str, pr_exam, now) -> str:
    """Write the graded attempt's learning records; "" or why it failed."""
    try:
        with state_lock(cfg.state_dir, "exam-records.lock"):   # numbering is read-then-write
            pr_exam.record(Path(cfg.state_dir) / "exam-records" / repo, attempt_dir / "exam.json",
                           attempt_dir / "result.json", now().astimezone(timezone.utc).date().isoformat())
    except (ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        return f"recording failed: {exc}"
    return ""


def _graded(cfg, attempt_dir: Path, scored: dict, diff: str, io):
    """The change a scored attempt makes: records written once, in the same locked step
    that leaves grading, so a later status failure never writes them again. A fail queues
    the follow-up on the missed concepts."""
    def change(cur: dict, d: Path) -> None:
        if error := _record(cfg, attempt_dir, cur["repo"], io.pr_exam, io.now):
            cur.update(state=ExamState.ERROR, detail=error)
            return
        exam = json.loads((attempt_dir / "exam.json").read_text())
        cur.update(_graded_fields(cur, scored, exam))
        if scored["passed"]:
            (d / "passed.diff").write_text(diff)
            cur["state"] = ExamState.PASSED
        else:
            cur.update(state=ExamState.GENERATING, followup={"only": list(dict.fromkeys(scored["missed"]))})
    return change


def _failed(detail: str):
    def change(cur: dict, d: Path) -> None:
        cur.update(state=ExamState.ERROR, detail=detail)
    return change


def _generated(job: dict, outcome: str, diff: str, body: str, attempt_dir: Path):
    """The change a generation makes: a staged exam is published as the next attempt, in
    `attempt_dir`, or a delta with no new concept passes the new head."""
    def change(cur: dict, d: Path) -> None:
        if outcome == "passed":
            (d / "passed.diff").write_text(diff)
            cur.update(state=ExamState.PASSED, head_sha=job["head_sha"], delta=False)
            return
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (d / STAGED[0]).replace(attempt_dir / "exam.json")
        (d / STAGED[1]).replace(attempt_dir / "public.json")
        (d / "diff.patch").write_text(diff)   # what the exam's chat is given: the head's diff and body
        (d / "body.md").write_text(body)
        if cur["delta"]:   # a delta exam starts over on the new head
            cur["results"] = []
        cur.update(state=ExamState.OPEN, head_sha=job["head_sha"], attempt=job["attempt"] + 1,
                   delta=False, followup=None, bypassed=None)
    return change


def _released(cur: dict, d: Path) -> None:
    """A job that raised leaves the exam as it was: the next work claims it again."""


def _execute_grade(cfg, doc, job: dict, model: str, io):
    """Grade and score the attempt the job was claimed on."""
    adir = attempt_dir(cfg.state_dir, doc["repo"], doc["pr"], job["attempt"])
    diff = io.github.diff(doc["repo"], doc["pr"])
    grades, error = _grade(cfg, doc, adir, job["head_sha"], diff, model, io)
    if grades is None:
        return _failed(error)
    scored, error = _score(adir, grades, io.pr_exam)
    return _failed(error) if scored is None else _graded(cfg, adir, scored, diff, io)


def _execute(cfg, doc, job: dict, pr, model: str, io):
    """Run the claimed job's session; the change its result makes."""
    if job["kind"] == "grade":
        return _execute_grade(cfg, doc, job, model, io)
    _save_app(cfg, io.github)
    _mark_started(cfg, doc, job["head_sha"], io.github)
    outcome, detail, diff = _generate(cfg, doc, job, pr, model, io)
    if outcome == "error":
        return _failed(detail)
    return _generated(job, outcome, diff, pr["body"] or "", attempt_dir(cfg.state_dir, doc["repo"], doc["pr"], job["attempt"] + 1))


def _save_app(cfg, github) -> None:
    with state_lock(cfg.state_dir, LOCK):
        info = github.app()
        write_json_atomic(Path(cfg.state_dir) / APP, {"app_slug": info["slug"], "app_id": info["id"]})


def _mark_started(cfg, doc, sha: str, github) -> None:
    """Post what a generating exam earns on `sha` before its session starts: a head no
    request named is pending from then on."""
    with state_lock(cfg.state_dir, LOCK):
        cur = _load(cfg.state_dir, doc["repo"], doc["pr"])
        cur["due"] = exam_lifecycle.status(cur, sha, cfg.console_url)
        _write(cfg.state_dir, cur)
    if _due(cur):
        _post_due(cfg, cur, github)


def _job(doc: dict, pr: dict, now) -> dict:
    kind = _kind(doc)
    return {"kind": kind, "head_sha": doc["head_sha"] if kind in FIXED else pr["head_sha"],
            "attempt": doc["attempt"], "started": now().isoformat()}


def _settle(cfg, doc, job: dict, change, pr: dict, io) -> bool:
    """End the job by `pr`, read after its session: its result is applied unless the head
    moved under it, and the exam is brought in line with that PR. True when it left a
    result rather than an error or a drop."""
    moved = pr["head_sha"] != job["head_sha"]
    after = _finish(cfg, doc, job, exam_lifecycle.new_head if moved else change)
    left = after is not None and not moved and _announce(cfg, job["kind"], after, io)
    _reconcile(cfg, doc, pr, io)
    return left


def _announce(cfg, kind: str, after: dict, io) -> bool:
    """Tell the operator what an applied job left; True when it left a result."""
    _tell_failure(cfg, after, io.notify)
    if after["state"] == ExamState.ERROR:
        return False
    ref = {"repo": after["repo"], "pr": after["pr"]}
    if kind == "grade":
        last = after["results"][-1]
        io.notify("exam_result", **ref, passed=last["passed"], score=last["score"], max=last["max"])
    if after["state"] == ExamState.OPEN:
        questions = public(cfg.state_dir, after["repo"], after["pr"], after["attempt"])["questions"]
        io.notify("exam_ready", **ref, questions=len(questions), debt=after["debt"])
    return True


def _tell_failure(cfg, doc: dict, notify) -> None:
    """Say why the exam's work failed, once per reason: `notified_error` is the reason the
    operator was told, kept over the retries and forgotten when a job leaves a result."""
    reason = doc["detail"] if doc["state"] == ExamState.ERROR else None
    if reason == doc.get("notified_error"):
        return
    _update(cfg.state_dir, doc["repo"], doc["pr"], notified_error=reason)
    if reason:
        notify("exam_failed", repo=doc["repo"], pr=doc["pr"], reason=reason)


def _run_job(cfg, doc, io) -> bool:
    """Claim, run and end this exam's next job; True when it left a result."""
    if doc["state"] == ExamState.NEW:
        doc = _update(cfg.state_dir, doc["repo"], doc["pr"], state=ExamState.GENERATING)
    chosen = entry(cfg, io.usages, io.now())
    if chosen is None:
        return False
    pr = io.github.pr(doc["repo"], doc["pr"])
    job = _job(doc, pr, io.now)
    if not _claim(cfg, doc, job, io.now):
        return False
    try:
        change = _execute(cfg, doc, job, pr, chosen.model_id, io)
        pr = io.github.pr(doc["repo"], doc["pr"])   # after the session: has its head moved, is it merged
    except BaseException:
        _finish(cfg, doc, job, _released)
        raise
    return _settle(cfg, doc, job, change, pr, io)


def _post_due(cfg, doc, github) -> None:
    """Post the queued status; it is marked posted only once GitHub took it."""
    due = doc["due"]
    github.post_status(doc["repo"], due["sha"], "exam", due["state"], due["description"],
                       target_url=due.get("target_url", ""))
    with state_lock(cfg.state_dir, LOCK):
        cur = _load(cfg.state_dir, doc["repo"], doc["pr"])
        if cur["due"] == due:
            cur["status"] = due
            _write(cfg.state_dir, cur)


def _reconcile(cfg, doc: dict, pr: dict, io) -> None:
    """Bring the exam in line with its PR as `pr`, read after `doc`, shows it. Only the
    requests `doc` counts are answered, and only once nothing holds the exam: a request
    made since, or during a job, stays wanted. The operator hears of a bypass that failed."""
    bypass = exam_lifecycle.bypass(cfg, doc["repo"], doc["pr"], pr, io)
    with state_lock(cfg.state_dir, LOCK):
        cur = _load(cfg.state_dir, doc["repo"], doc["pr"])
        if cur is None:
            return
        d = _dir(cfg.state_dir, doc["repo"], doc["pr"])
        if exam_lifecycle.reconcile(cur, d, pr, bypass, cfg.console_url):
            shutil.rmtree(d, ignore_errors=True)
            return
        if not exam_lifecycle.busy(cur):
            cur.update(reconciled=max(cur["reconciled"], doc["requested"]), stale=False)
        _write(cfg.state_dir, cur)
    if cur["state"] == ExamState.ERROR:   # a config or a label that cannot be honoured fails like a job
        _tell_failure(cfg, cur, io.notify)


def _step(cfg, doc: dict, io) -> bool:
    """Do this exam's next piece of work; True when another may follow at once."""
    if _due(doc):
        _post_due(cfg, doc, io.github)
        return True
    return bool(_kind(doc)) and _run_job(cfg, doc, io)


def _work_exam(cfg, doc: dict, io) -> None:
    """Answer the exam's request, then post its queued status and run its jobs."""
    if _wanted(doc):
        _reconcile(cfg, doc, io.github.pr(doc["repo"], doc["pr"]), io)
    for _ in range(10):   # a pass is at most status, grade, status, follow-up, status
        doc = read(cfg.state_dir, doc["repo"], doc["pr"])
        if doc is None or not _step(cfg, doc, io):
            return


def _sweep_exam(cfg, doc: dict, io) -> None:
    _reconcile(cfg, doc, io.github.pr(doc["repo"], doc["pr"]), io)


def _each(cfg, io, do) -> None:
    """`do(cfg, doc, io)` for every exam: one exam's GitHub or script failure never stops
    another's work."""
    for doc in _exams(cfg.state_dir):
        try:
            do(cfg, doc, io)
        except Exception as exc:  # noqa: BLE001 — the job was released; the next work retries
            print(f"exam work for {doc['repo']}#{doc['pr']} failed: {exc}", file=sys.stderr)


def work(cfg, *, github, run, pr_exam, notify, now, usages) -> None:
    """Do the pending work of every exam: answer its request by its PR, post queued
    statuses and run its jobs. Runs outside task capacity."""
    io = SimpleNamespace(github=github, run=run, pr_exam=pr_exam, notify=notify, now=now, usages=usages)
    _each(cfg, io, _work_exam)


def sweep(cfg, **edges) -> None:
    """The daily sweep, with the edges `work` takes: bring every exam in line with its PR,
    requested or not, then do the work that leaves."""
    _each(cfg, SimpleNamespace(**edges), _sweep_exam)
    work(cfg, **edges)




# Chat turns. Which attempt takes which turn is decided here, under the exams lock, by the same
# predicates the view serves (`_chatting`, `_taking`); `exam_threads` keeps the turns.

def _admit(state_dir, doc: dict | None, ask: Ask) -> Path:
    """The directory of the attempt `ask` names, when it may take that turn now; else Refused."""
    adir = _open_to(state_dir, doc, ask)
    if ask.question is not None:
        _require(ask.question, public(state_dir, doc["repo"], doc["pr"], ask.attempt)["questions"])
    _free(exam_threads.turns(adir), ask)
    return adir


def _open_to(state_dir, doc: dict | None, ask: Ask) -> Path:
    """The directory of the attempt `ask` names, when that attempt of this head takes its kind
    of turn now: `_chatting`, and `_taking` for a tip. Else Refused."""
    if doc is None or doc["head_sha"] != ask.head_sha:
        raise Refused(Refusal.CONFLICT, "this attempt cannot take that turn")
    adir = attempt_dir(state_dir, doc["repo"], doc["pr"], ask.attempt)
    if not (_chatting(adir, doc, ask.attempt) and (ask.kind != "tip" or _taking(doc, ask.attempt))):
        raise Refused(Refusal.CONFLICT, "this attempt cannot take that turn")
    return adir


def _free(turns: list, ask: Ask) -> None:
    """Refused while a turn runs in the thread `ask` names, or for a tip past the cap."""
    if exam_threads.running(turns, ask.question):
        raise Refused(Refusal.BUSY, "a turn is already running there")
    if ask.kind == "tip" and exam_threads.tips_held(turns, ask.question) >= exam_threads.TIPS:
        raise Refused(Refusal.CONFLICT, f"this question has had its {exam_threads.TIPS} tips")


def prepare_turn(state_dir, repo: str, pr: int, ask: Ask) -> Prompt:
    """What the skill's `context` takes for `ask`, built from the files of the attempt it names;
    Refused when that attempt may not take the turn. Changes nothing: `start_turn` reserves it."""
    with state_lock(state_dir, LOCK):
        adir = _admit(state_dir, _load(state_dir, repo, pr), ask)
        d = _dir(state_dir, repo, pr)
        return Prompt(adir / "exam.json", d / "diff.patch", d / "body.md", exam_threads.document(adir, ask))


def start_turn(state_dir, repo: str, pr: int, ask: Ask) -> str:
    """Reserve `ask` as a running turn, checked again as `prepare_turn` did; its id, or Refused."""
    with state_lock(state_dir, LOCK):
        return exam_threads.reserve(_admit(state_dir, _load(state_dir, repo, pr), ask), ask)


def end_turn(state_dir, repo: str, pr: int, attempt: int, turn_id: str, text: str, error: str) -> str:
    """Store a turn's reply, "done" or "failed" by `error`; "exam changed" when its attempt is
    gone (the head moved or the exam was deleted) and the reply is dropped. A turn that ends
    once its attempt is no longer open is `after_submit`: it marks nothing."""
    with state_lock(state_dir, LOCK):
        doc = _load(state_dir, repo, pr)
        adir = attempt_dir(state_dir, repo, pr, attempt)
        if doc is None or not exam_threads.end(adir, turn_id, text, error, not _taking(doc, attempt)):
            return "exam changed"
        return ""


def fail_running_turns(state_dir) -> None:
    """The console's startup: a turn still running was lost with the console that ran it."""
    with state_lock(state_dir, LOCK):
        for threads in sorted(Path(state_dir, "exams").glob("*/*/*/attempts/*/threads.json")):
            exam_threads.fail_running(threads.parent)
