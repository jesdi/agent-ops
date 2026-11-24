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
from types import SimpleNamespace
from typing import Iterator

from dispatcher import exam_lifecycle, priority
from dispatcher.config import routed_providers
from dispatcher.exam_lifecycle import ExamState
from dispatcher.models import triage_entry
from dispatcher.state import state_lock, write_json_atomic
from dispatcher.usage import admits

LOCK = "exams.lock"
TIMEOUT = 1200   # a session runs 20 minutes at most
APP = "exam-app.json"
STAGED = ("next-exam.json", "next-public.json")   # a generated exam before it is applied


def _path(state_dir: str | Path, repo: str, pr: int) -> Path:
    return Path(state_dir) / "exams" / repo / str(pr) / exam_lifecycle.STATE_FILE


def _dir(state_dir, repo: str, pr: int) -> Path:
    return _path(state_dir, repo, pr).parent


ATTEMPT_FILES = ("exam.json", "public.json", "answers.json", "grades.json", "result.json")


def _attempt_dir(d: Path, n: int) -> Path:
    """Where attempt `n` keeps its exam, answers, grades and result."""
    return d / "attempts" / str(n)


def _migrate(d: Path, attempt: int) -> None:
    """Move an exam left in the flat layout (per-attempt files at the exam level) into its
    attempt's directory. The caller holds the exams lock. Each file moves by an atomic rename
    and any file left at the flat path asks for the move, so an interrupted move resumes."""
    if not any((d / name).exists() for name in ATTEMPT_FILES):
        return
    target = _attempt_dir(d, attempt)
    target.mkdir(parents=True, exist_ok=True)
    for name in ATTEMPT_FILES:
        try:
            (d / name).replace(target / name)
        except FileNotFoundError:
            pass


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
    if doc["attempt"]:
        _migrate(_path(state_dir, repo, pr).parent, doc["attempt"])
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
                   "results": []}
        elif doc["state"] == ExamState.ERROR and not doc["grading"]:
            doc["state"] = ExamState.NEW   # a request after a failed job retries it
        doc["requested"] += 1
        _write(state_dir, doc)
        return doc


def public(state_dir: str | Path, repo: str, pr: int, attempt: int | None = None) -> dict | None:
    """The served questions of `attempt` (the current one), as pr_exam.public projected them;
    never the exam file."""
    if attempt is None:
        doc = read(state_dir, repo, pr)
        attempt = doc["attempt"] if doc else 0
    try:
        return json.loads((_attempt_dir(_dir(state_dir, repo, pr), attempt) / "public.json").read_text())
    except FileNotFoundError:
        return None


def kept(state_dir: str | Path, doc: dict) -> list[dict]:
    """The failed attempts before the current one, oldest first, with their questions and result."""
    out = []
    for result in doc["results"]:
        n = result["attempt"]
        if result["passed"] or n >= doc["attempt"]:
            continue
        if questions := public(state_dir, doc["repo"], doc["pr"], n):
            out.append({"attempt": n, "questions": questions["questions"], "result": result})
    return out


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
    if _kind(cur) != job["kind"] or _live(cur["job"], now):
        return False
    return job["kind"] not in FIXED or (cur["head_sha"], cur["attempt"]) == (job["head_sha"], job["attempt"])


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
    prior = json.loads((_attempt_dir(d, doc["attempt"]) / "exam.json").read_text()) if job["kind"] == "followup" else None
    spec = _spec(cfg, doc, job["kind"], job["head_sha"], jd, glossary, io.now, prior)
    if error := _exam_error(io, jd, spec, model):
        return "error", error, diff
    out = jd / "exam.json"
    if job["kind"] == "delta" and not json.loads(out.read_text())["questions"]:
        return "passed", "", diff
    write_json_atomic(d / STAGED[1], io.pr_exam.public(out))
    shutil.copyfile(out, d / STAGED[0])
    return "open", "", diff


def _entry(cfg, usages, when):
    return triage_entry(
        cfg.models, lambda m: admits(usages, m, when, cfg.pace).admitted,
        priority.order(priority.load(cfg.state_dir, routed_providers(cfg)),
                       usages, when, cfg.pace))


def _is_open(doc: dict | None, attempt: int, head_sha: str) -> bool:
    return (doc is not None and doc["state"] == ExamState.OPEN and doc["attempt"] == attempt
            and doc["head_sha"] == head_sha)


def _answers(q: dict, a) -> bool:
    """A choice is answered by an int, a text question by a string."""
    return not isinstance(a, bool) and isinstance(a, (int, str)) and isinstance(a, int) == (q["type"] == "choice")


def submit(state_dir, repo: str, pr: int, attempt: int, head_sha: str, answers: dict) -> str:
    """Accept the answers of the open attempt: "ok", "conflict" (repeated, earlier or
    another head) or "incomplete". Changes nothing unless "ok"."""
    with state_lock(state_dir, LOCK):
        doc = _load(state_dir, repo, pr)
        if not _is_open(doc, attempt, head_sha):
            return "conflict"
        questions = public(state_dir, repo, pr, doc["attempt"])["questions"]
        if not all(_answers(q, answers.get(q["id"])) for q in questions):
            return "incomplete"
        write_json_atomic(_attempt_dir(_dir(state_dir, repo, pr), attempt) / "answers.json", {q["id"]: answers[q["id"]] for q in questions})
        doc.update(state=ExamState.GRADING, grading={"attempt": attempt})
        _write(state_dir, doc)
        return "ok"


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
    """The grading session's grades of `texts`; (None, error) unless every one is well formed."""
    if out.is_symlink() or not out.is_file():
        return None, "the grading session wrote no regular grades file"
    try:
        grades = json.loads(out.read_text())
        if all(grades[q["id"]]["grade"] in ("pass", "partial", "fail") for q in texts):
            return grades, ""
    except (ValueError, KeyError, TypeError):
        pass
    return None, "the grading session's grades are malformed"


def _grade(cfg, doc, sha, diff: str, model, io) -> tuple[dict | None, str]:
    """Grades of the free-text answers by a fresh session; (grades, error)."""
    d = _dir(cfg.state_dir, doc["repo"], doc["pr"])
    a = _attempt_dir(d, doc["attempt"])
    key = json.loads((a / "exam.json").read_text())
    texts = [q for q in key["questions"] if q["type"] == "text"]
    if not texts:
        return {}, ""
    answers = json.loads((a / "answers.json").read_text())
    job = _job_dir(d)
    request = _grading_request(job, doc, sha, diff, key, answers, texts)
    if error := _run(io, request, model, "the grading session"):
        return None, error
    return _grades(job / "grades.json", texts)


def _score(d: Path, grades: dict, pr_exam) -> tuple[dict | None, str]:
    """Score the graded attempt into result.json; (scored, error)."""
    (d / "grades.json").write_text(json.dumps(grades))
    try:
        scored = pr_exam.score(d / "exam.json", d / "answers.json", d / "grades.json")
    except (ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        return None, f"scoring failed: {exc}"
    for q in scored["questions"]:   # the grader's line rides along with each free-text question
        if feedback := _feedback(grades.get(q["id"])):
            q["feedback"] = feedback
    (d / "result.json").write_text(json.dumps(scored))
    return scored, ""


def _feedback(grade) -> str:
    """The grader's feedback on one question, capped; "" when it gave none as text."""
    feedback = grade.get("feedback") if isinstance(grade, dict) else None
    return feedback[:1000] if isinstance(feedback, str) else ""


def _result(doc, scored: dict) -> dict:
    """The attempt as the page shows it: explanations only where points were lost."""
    return {"attempt": doc["attempt"], "passed": scored["passed"], "score": scored["score"],
            "max": scored["max"], "questions": [
                {k: v for k, v in q.items() if k in ("id", "concept", "points")
                 or (q["points"] < 1 and k in ("explanation", "hunk", "feedback"))} for q in scored["questions"]]}


def _graded_fields(doc, scored: dict, exam: dict) -> dict:
    missed = set() if scored["passed"] else set(scored["missed"])   # missed concepts return in the follow-up
    return {"grading": None, "results": [*doc["results"], _result(doc, scored)],
            "passed_concepts": list(dict.fromkeys(
                [*doc["passed_concepts"], *(c["id"] for c in exam["concepts"] if c["id"] not in missed)]))}


def _record(cfg, d: Path, repo: str, pr_exam, now) -> str:
    """Write the graded attempt's learning records; "" or why it failed."""
    try:
        with state_lock(cfg.state_dir, "exam-records.lock"):   # numbering is read-then-write
            pr_exam.record(Path(cfg.state_dir) / "exam-records" / repo, d / "exam.json",
                           d / "result.json", now().astimezone(timezone.utc).date().isoformat())
    except (ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        return f"recording failed: {exc}"
    return ""


def _graded(cfg, scored: dict, diff: str, io):
    """The change a scored attempt makes: records written once, in the same locked step
    that leaves grading, so a later status failure never writes them again. A fail queues
    the follow-up on the missed concepts."""
    def change(cur: dict, d: Path) -> None:
        if error := _record(cfg, _attempt_dir(d, cur["attempt"]), cur["repo"], io.pr_exam, io.now):
            cur.update(state=ExamState.ERROR, detail=error)
            return
        exam = json.loads((_attempt_dir(d, cur["attempt"]) / "exam.json").read_text())
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


def _generated(job: dict, outcome: str, diff: str):
    """The change a generation makes: a staged exam is published as the next attempt,
    or a delta with no new concept passes the new head."""
    def change(cur: dict, d: Path) -> None:
        if outcome == "passed":
            (d / "passed.diff").write_text(diff)
            cur.update(state=ExamState.PASSED, head_sha=job["head_sha"], delta=False)
            return
        a = _attempt_dir(d, cur["attempt"] + 1)
        a.mkdir(parents=True, exist_ok=True)
        (d / STAGED[0]).replace(a / "exam.json")
        (d / STAGED[1]).replace(a / "public.json")
        if cur["delta"]:   # a delta exam starts over on the new head
            cur["results"] = []
        cur.update(state=ExamState.OPEN, head_sha=job["head_sha"], attempt=cur["attempt"] + 1,
                   delta=False, followup=None)
    return change


def _released(cur: dict, d: Path) -> None:
    """A job that raised leaves the exam as it was: the next work claims it again."""


def _execute_grade(cfg, doc, job: dict, model: str, io):
    diff = io.github.diff(doc["repo"], doc["pr"])
    grades, error = _grade(cfg, doc, job["head_sha"], diff, model, io)
    if grades is None:
        return _failed(error)
    scored, error = _score(_attempt_dir(_dir(cfg.state_dir, doc["repo"], doc["pr"]), doc["attempt"]),
                           grades, io.pr_exam)
    return _failed(error) if scored is None else _graded(cfg, scored, diff, io)


def _execute(cfg, doc, job: dict, pr, model: str, io):
    """Run the claimed job's session; the change its result makes."""
    if job["kind"] == "grade":
        return _execute_grade(cfg, doc, job, model, io)
    _save_app(cfg, io.github)
    _mark_started(cfg, doc, job["head_sha"], io.github)
    outcome, detail, diff = _generate(cfg, doc, job, pr, model, io)
    return _failed(detail) if outcome == "error" else _generated(job, outcome, diff)


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
        questions = public(cfg.state_dir, after["repo"], after["pr"])["questions"]
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
    entry = _entry(cfg, io.usages, io.now())
    if entry is None:
        return False
    pr = io.github.pr(doc["repo"], doc["pr"])
    job = _job(doc, pr, io.now)
    if not _claim(cfg, doc, job, io.now):
        return False
    try:
        change = _execute(cfg, doc, job, pr, entry.model_id, io)
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
