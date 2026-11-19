"""Exam state on the box: `<state_dir>/exams/<owner>/<name>/<pr>/state.json`.

Every write holds the exams lock and renames atomically. All exam work is one job
at a time per exam, `job = {kind, head_sha, attempt, started}`: claimed under the
lock, run outside it, and applied under the lock only while it is still the
recorded job. The kind selects the operation: `generate` (first exam), `delta`
(judgment after a pass), `followup` (missed concepts after a fail), `grade`."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from dispatcher import exam_bypass, priority
from dispatcher.config import routed_providers
from dispatcher.models import triage_entry
from dispatcher.state import state_lock, write_json_atomic
from dispatcher.usage import admits
from telegram.templates import render

LOCK = "exams.lock"
TIMEOUT = 1200   # a session runs 20 minutes at most
APP = "exam-app.json"
STAGED = ("next-exam.json", "next-public.json")   # a generated exam before it is applied
ATTEMPT_FILES = ("public.json", "exam.json", "answers.json", "grades.json", "result.json",
                 "passed.diff", *STAGED)


def _path(state_dir: str | Path, repo: str, pr: int) -> Path:
    return Path(state_dir) / "exams" / repo / str(pr) / "state.json"


def _dir(state_dir, repo: str, pr: int) -> Path:
    return _path(state_dir, repo, pr).parent


def read(state_dir: str | Path, repo: str, pr: int) -> dict | None:
    try:
        return json.loads(_path(state_dir, repo, pr).read_text())
    except FileNotFoundError:
        return None


def _write(state_dir, doc: dict) -> None:
    write_json_atomic(_path(state_dir, doc["repo"], doc["pr"]), doc, indent=2)


def request(state_dir: str | Path, repo: str, pr: int) -> dict:
    """Record that this PR's exam is wanted; a repeat finds the same state. Each request
    raises `requested`; work reconciles with GitHub until `reconciled` catches up."""
    with state_lock(state_dir, LOCK):
        doc = read(state_dir, repo, pr)
        if doc is None:
            doc = {"repo": repo, "pr": pr, "state": "new", "head_sha": None, "debt": False,
                   "detail": "", "attempt": 0, "requested": 0, "reconciled": 0, "job": None,
                   "status": None, "due": None, "merged": False, "passed_concepts": [],
                   "grading": None, "followup": None, "delta": False, "results": []}
        elif doc["state"] in ("skipped", "disabled") or (doc["state"] == "error" and not doc["grading"]):
            # a request after a failure retries; a bypass or a disabled repo is decided again
            doc["state"] = "new"
        doc["requested"] += 1
        _write(state_dir, doc)
        return doc


def public(state_dir: str | Path, repo: str, pr: int) -> dict | None:
    """The served questions, as pr_exam.public projected them; never the exam file."""
    try:
        return json.loads((_dir(state_dir, repo, pr) / "public.json").read_text())
    except FileNotFoundError:
        return None


def app(state_dir: str | Path) -> dict | None:
    try:
        return json.loads((Path(state_dir) / APP).read_text())
    except FileNotFoundError:
        return None


def pending(state_dir: str | Path) -> bool:
    """True when some exam has work for the runner."""
    for path in Path(state_dir, "exams").glob("*/*/*/state.json"):
        doc = json.loads(path.read_text())
        if _kind(doc) or _wanted(doc) or _due(doc):
            return True
    return False


def _update(state_dir, repo: str, pr: int, **fields) -> dict:
    with state_lock(state_dir, LOCK):
        doc = read(state_dir, repo, pr)
        doc.update(fields)
        _write(state_dir, doc)
        return doc


def _wanted(doc: dict) -> bool:
    return doc["requested"] > doc["reconciled"]


def _due(doc: dict) -> dict | None:
    """The status still to post, None when it is posted."""
    return doc["due"] if doc["due"] and doc["due"] != doc["status"] else None


def _kind(doc: dict) -> str | None:
    """The job this exam's state asks for; None when it waits for the operator or a request."""
    if doc["grading"]:
        return "grade"
    if doc["state"] not in ("new", "generating"):
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
        cur = read(cfg.state_dir, doc["repo"], doc["pr"])
        if cur is None or not _claimable(cur, job, now):
            return False
        if job["kind"] not in FIXED:   # the head was read after these requests
            cur["reconciled"] = max(cur["reconciled"], doc["requested"])
        cur.update(job=job, detail="", state="grading" if job["kind"] == "grade" else "generating")
        _write(cfg.state_dir, cur)
        return True


def _apply(cfg, doc: dict, job: dict, change) -> bool:
    """Apply `change(cur, d)` to the exam under the lock, only while `job` is its job."""
    with state_lock(cfg.state_dir, LOCK):
        cur = read(cfg.state_dir, doc["repo"], doc["pr"])
        if cur is None or cur["job"] != job:
            return False
        cur["job"] = None
        change(cur, _dir(cfg.state_dir, doc["repo"], doc["pr"]))
        if cur["state"] != "error" and change not in (_keep, _moved):
            cur.pop("notified_error", None)   # only a result resolves a failure; a release or a drop does not
        _write(cfg.state_dir, cur)
        return True


def _drop_attempts(cur: dict, d: Path) -> None:
    """Drop every attempt of an exam whose head moved and send it back to generating."""
    for name in ATTEMPT_FILES:
        (d / name).unlink(missing_ok=True)
    shutil.rmtree(d / "job", ignore_errors=True)
    cur.update(state="generating", head_sha=None, attempt=0, results=[], passed_concepts=[],
               status=None, grading=None, followup=None, delta=False, detail="", job=None)


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
    prior = json.loads((d / "exam.json").read_text()) if job["kind"] == "followup" else None
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
    return (doc is not None and doc["state"] == "open" and doc["attempt"] == attempt
            and doc["head_sha"] == head_sha)


def _answers(q: dict, a) -> bool:
    """A choice is answered by an int, a text question by a string."""
    return not isinstance(a, bool) and isinstance(a, (int, str)) and isinstance(a, int) == (q["type"] == "choice")


def submit(state_dir, repo: str, pr: int, attempt: int, head_sha: str, answers: dict) -> str:
    """Accept the answers of the open attempt: "ok", "conflict" (repeated, earlier or
    another head) or "incomplete". Changes nothing unless "ok"."""
    with state_lock(state_dir, LOCK):
        doc = read(state_dir, repo, pr)
        if not _is_open(doc, attempt, head_sha):
            return "conflict"
        questions = public(state_dir, repo, pr)["questions"]
        if not all(_answers(q, answers.get(q["id"])) for q in questions):
            return "incomplete"
        write_json_atomic(_dir(state_dir, repo, pr) / "answers.json", {q["id"]: answers[q["id"]] for q in questions})
        doc.update(state="grading", grading={"attempt": attempt})
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
    key = json.loads((d / "exam.json").read_text())
    texts = [q for q in key["questions"] if q["type"] == "text"]
    if not texts:
        return {}, ""
    answers = json.loads((d / "answers.json").read_text())
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
    (d / "result.json").write_text(json.dumps(scored))
    return scored, ""


def _result(doc, scored: dict) -> dict:
    """The attempt as the page shows it: explanations only where points were lost."""
    return {"attempt": doc["attempt"], "passed": scored["passed"], "score": scored["score"],
            "max": scored["max"], "questions": [
                {k: v for k, v in q.items() if k in ("id", "concept", "points")
                 or (q["points"] < 1 and k in ("explanation", "hunk"))} for q in scored["questions"]]}


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


def _status(sha: str, state: str, description: str) -> dict:
    return {"sha": sha, "state": state, "description": description}


def _due_status(cur: dict, sha: str, description: str, merged: bool = False) -> dict | None:
    """The success status to queue; a merged PR gets none, its exam is only debt."""
    return None if cur["merged"] or merged else _status(sha, "success", description)


def _graded(cfg, job: dict, scored: dict, diff: str, io, merged: bool = False):
    """The change a scored attempt makes: records written once, in the same locked step
    that leaves grading, so a later status failure never writes them again. A pass
    queues its status; a fail queues the follow-up on the missed concepts."""
    def change(cur: dict, d: Path) -> None:
        if error := _record(cfg, d, cur["repo"], io.pr_exam, io.now):
            cur.update(state="error", detail=error)
            return
        exam = json.loads((d / "exam.json").read_text())
        cur.update(_graded_fields(cur, scored, exam))
        if scored["passed"]:
            (d / "passed.diff").write_text(diff)
            cur.update(state="passed", due=_due_status(cur, job["head_sha"], "Exam passed", merged))
        else:
            cur.update(state="generating", followup={"only": list(dict.fromkeys(scored["missed"]))})
    return change


def _moved(cur: dict, d: Path) -> None:
    """A job's head moved: an attempt's work is dropped with its attempts; a generation
    or delta judgment is run again on the new head."""
    if cur["grading"] or cur["followup"]:
        _drop_attempts(cur, d)
    else:
        cur["state"] = "generating"


def _failed(detail: str):
    def change(cur: dict, d: Path) -> None:
        cur.update(state="error", detail=detail)
    return change


def _generated(job: dict, outcome: str, diff: str):
    """The change a generation makes: a staged exam is published as the next attempt,
    or a delta with no new concept passes the new head."""
    def change(cur: dict, d: Path) -> None:
        sha = job["head_sha"]
        if outcome == "passed":
            (d / "passed.diff").write_text(diff)
            cur.update(state="passed", head_sha=sha, delta=False,
                       due=_due_status(cur, sha, "Exam passed"))
            return
        (d / STAGED[0]).replace(d / "exam.json")
        (d / STAGED[1]).replace(d / "public.json")
        if cur["delta"]:   # a delta exam starts over on the new head
            cur["results"] = []
        cur.update(state="open", head_sha=sha, attempt=cur["attempt"] + 1, delta=False, followup=None)
        if cur["debt"]:
            cur["due"] = _due_status(cur, sha, "deferred")
    return change


def _head_moved(io, doc, job: dict) -> bool:
    return io.github.pr(doc["repo"], doc["pr"])["head_sha"] != job["head_sha"]


def _execute_grade(cfg, doc, job: dict, model: str, io):
    diff = io.github.diff(doc["repo"], doc["pr"])
    grades, error = _grade(cfg, doc, job["head_sha"], diff, model, io)
    pr = io.github.pr(doc["repo"], doc["pr"])   # read after the session: a merge during grading queues no status
    if pr["head_sha"] != job["head_sha"]:
        return _moved
    if grades is None:
        return _failed(error)
    scored, error = _score(_dir(cfg.state_dir, doc["repo"], doc["pr"]), grades, io.pr_exam)
    return _failed(error) if scored is None else _graded(cfg, job, scored, diff, io, pr["merged"])


def _execute(cfg, doc, job: dict, pr, model: str, io):
    """Run the claimed job's session; the change to apply. The head is checked after
    the session: a result for a head that moved is dropped."""
    if job["kind"] == "grade":
        return _execute_grade(cfg, doc, job, model, io)
    _save_app(cfg, io.github)
    _mark_pending(cfg, read(cfg.state_dir, doc["repo"], doc["pr"]), io.github, job["head_sha"])
    outcome, detail, diff = _generate(cfg, doc, job, pr, model, io)
    if _head_moved(io, doc, job):
        return _moved
    return _failed(detail) if outcome == "error" else _generated(job, outcome, diff)


def _save_app(cfg, github) -> None:
    with state_lock(cfg.state_dir, LOCK):
        info = github.app()
        write_json_atomic(Path(cfg.state_dir) / APP, {"app_slug": info["slug"], "app_id": info["id"]})


def _mark_pending(cfg, doc, github, sha: str) -> None:
    """Post the pending status as the App, once per head."""
    status = _status(sha, "pending", "Exam in progress")
    if doc["status"] != status:
        github.post_status(doc["repo"], sha, "exam", "pending", "Exam in progress")
        _update(cfg.state_dir, doc["repo"], doc["pr"], status=status)


def _keep(cur: dict, d: Path) -> None:
    pass


def _release(cfg, doc, job: dict) -> None:
    """Give up a job that raised: the next work claims it again."""
    _apply(cfg, doc, job, _keep)


def _job(doc: dict, pr: dict, now) -> dict:
    kind = _kind(doc)
    return {"kind": kind, "head_sha": doc["head_sha"] if kind in FIXED else pr["head_sha"],
            "attempt": doc["attempt"], "started": now().isoformat()}


def _url(cfg, doc: dict) -> str:
    return f"{cfg.console_url}/exam/{doc['repo']}/{doc['pr']}"


def _settle(cfg, doc, job: dict, change, io) -> bool:
    """Apply the job's change; True when it left a result rather than an error or a drop.
    The operator hears of a ready exam, a graded attempt and a new failure."""
    applied = _apply(cfg, doc, job, change)
    after = read(cfg.state_dir, doc["repo"], doc["pr"])
    if not applied or after is None or change is _moved:
        return False
    return _announce(cfg, job["kind"], after, io.notify)


def _announce(cfg, kind: str, after: dict, notify) -> bool:
    """Tell the operator what an applied job left; True when it left a result."""
    if after["state"] == "error":
        _notify_failed(cfg, after, notify)
        return False
    if kind == "grade":
        _notify_result(cfg, after, notify)
    if after["state"] == "open":
        _notify_ready(cfg, after, notify)
    return True


def _notify_ready(cfg, doc: dict, notify) -> None:
    if cfg.console_url:
        notify(render("exam_ready", repo=doc["repo"], pr=doc["pr"], url=_url(cfg, doc), debt=doc["debt"],
                      questions=len(public(cfg.state_dir, doc["repo"], doc["pr"])["questions"])))


def _notify_result(cfg, doc: dict, notify) -> None:
    if cfg.console_url:
        r = doc["results"][-1]
        notify(render("exam_result", repo=doc["repo"], pr=doc["pr"], passed=r["passed"],
                      score=r["score"], max=r["max"], url=_url(cfg, doc)))


def _notify_failed(cfg, doc: dict, notify) -> None:
    """One message per reason: a retry that fails the same way stays quiet."""
    if doc["detail"] == doc.get("notified_error"):
        return
    _update(cfg.state_dir, doc["repo"], doc["pr"], notified_error=doc["detail"])
    notify(render("exam_failed", repo=doc["repo"], pr=doc["pr"], reason=doc["detail"]))


def _run_job(cfg, doc, io) -> bool:
    """Claim, run and apply this exam's next job; True when it was applied as a result."""
    if doc["state"] == "new":
        doc = _update(cfg.state_dir, doc["repo"], doc["pr"], state="generating")
    entry = _entry(cfg, io.usages, io.now())
    if entry is None:
        return False
    pr = io.github.pr(doc["repo"], doc["pr"])
    job = _job(doc, pr, io.now)
    if not _claim(cfg, doc, job, io.now):
        return False
    try:
        change = _execute(cfg, doc, job, pr, entry.model_id, io)
    except BaseException:
        _release(cfg, doc, job)
        raise
    return _settle(cfg, doc, job, change, io)


def _post_due(cfg, doc, github) -> None:
    """Post the queued status; it is marked posted only once GitHub took it."""
    due = doc["due"]
    github.post_status(doc["repo"], due["sha"], "exam", due["state"], due["description"])
    with state_lock(cfg.state_dir, LOCK):
        cur = read(cfg.state_dir, doc["repo"], doc["pr"])
        if cur["due"] == due:
            cur["status"] = due
            _write(cfg.state_dir, cur)


def _reconcile(cfg, doc, head: str) -> bool:
    """Bring an open or passed exam in line with the PR's head after a request; True when
    the head moved (an open exam is replaced, a passed one gets a delta judgment). Only
    the requests made before GitHub was read are marked reconciled."""
    with state_lock(cfg.state_dir, LOCK):
        cur = read(cfg.state_dir, doc["repo"], doc["pr"])
        if cur["state"] not in ("open", "passed") or cur["job"]:
            return False
        cur["reconciled"] = max(cur["reconciled"], doc["requested"])
        moved = head != cur["head_sha"]
        if moved:
            _moved_head(cur, _dir(cfg.state_dir, doc["repo"], doc["pr"]))
        _write(cfg.state_dir, cur)
        return moved


def _moved_head(cur: dict, d: Path) -> None:
    if cur["state"] == "passed":
        cur.update(state="generating", delta=True)
    else:
        _drop_attempts(cur, d)


def _busy(doc: dict) -> bool:
    """A job or a grading holds the exam."""
    return bool(doc["job"] or doc["grading"])


def _bypass_skip(cur: dict, d: Path, pr: dict, detail: str) -> bool:
    _drop_attempts(cur, d)
    cur.update(state="skipped", head_sha=pr["head_sha"],
               due=_status(pr["head_sha"], "success", "bypassed"))
    return True


def _bypass_disabled(cur: dict, d: Path, pr: dict, detail: str) -> bool:
    _drop_attempts(cur, d)
    cur.update(state="disabled", head_sha=pr["head_sha"], due=None)
    return True


def _bypass_defer(cur: dict, d: Path, pr: dict, detail: str) -> bool:
    cur["title"] = pr["title"]   # the debt list shows it
    if cur["state"] != "open":
        return False
    cur["due"] = _status(pr["head_sha"], "success", "deferred")
    return True


def _bypass_error(cur: dict, d: Path, pr: dict, detail: str) -> bool:
    cur.update(state="error", detail=detail)
    return True


def _bypass_none(cur: dict, d: Path, pr: dict, detail: str) -> bool:
    return False


# what each mode of exam_bypass.decide does to the exam; True when it settled the request
_BYPASSES = {"skip": _bypass_skip, "disabled": _bypass_disabled, "defer": _bypass_defer,
             "error": _bypass_error, "none": _bypass_none}


def _bypass(cfg, doc, pr: dict, io) -> bool:
    """Settle a request by the owner's bypass labels and the repo's config; True when it
    did, by `pr`, read after `doc`. A skip drops the exam and queues "bypassed"; a defer marks the exam as debt and
    keeps an open one, queuing "deferred" on the head; an error or a disabled repo posts
    nothing. Otherwise the request goes on as it would without a label."""
    mode, detail = exam_bypass.decide(cfg, doc["repo"], doc["pr"], pr, io)
    with state_lock(cfg.state_dir, LOCK):
        cur = read(cfg.state_dir, doc["repo"], doc["pr"])
        if _busy(cur):
            return False
        cur["debt"] = mode == "defer" or cur["merged"]   # a merge without a pass stays debt
        settled = _BYPASSES[mode](cur, _dir(cfg.state_dir, doc["repo"], doc["pr"]), pr, detail)
        if settled:
            cur["reconciled"] = max(cur["reconciled"], doc["requested"])
        _write(cfg.state_dir, cur)
        return settled


def _lifecycle(cfg, doc, pr: dict) -> bool:
    """Keep the exam in step with its PR; True when the exam was deleted. A PR closed
    unmerged loses its exam. A merged PR loses a passed, bypassed or never-started exam
    (the records in exam-records stay); any other exam of a merged PR is debt until passed."""
    with state_lock(cfg.state_dir, LOCK):
        cur = read(cfg.state_dir, doc["repo"], doc["pr"])
        if cur is None:
            return True
        if _ended(cur, pr):
            shutil.rmtree(_dir(cfg.state_dir, doc["repo"], doc["pr"]), ignore_errors=True)
            return True
        if pr["merged"]:
            _mark_debt(cfg, cur, pr)
        return False


def _ended(cur: dict, pr: dict) -> bool:
    """Whether the PR's state ends the exam."""
    if pr["merged"]:
        return cur["state"] in ("passed", "skipped", "disabled") or _untouched(cur)
    return pr["state"] == "closed"


def _untouched(cur: dict) -> bool:
    """Never started: no attempt, no debt, no status and no job."""
    return cur["attempt"] == 0 and not cur["debt"] and cur["status"] is None and not cur["job"]


def _mark_debt(cfg, cur: dict, pr: dict) -> None:
    if not (cur["merged"] and cur["debt"]):
        _write(cfg.state_dir, {**cur, "merged": True, "debt": True, "title": pr["title"]})


def debts(state_dir: str | Path) -> list[dict]:
    """The exams that are open debt, as the console lists them."""
    docs = (json.loads(p.read_text()) for p in sorted(Path(state_dir, "exams").glob("*/*/*/state.json")))
    return [{k: d.get(k) for k in ("repo", "pr", "head_sha", "state", "debt", "title")}
            for d in docs if d["state"] == "open" and d["debt"]]


def _step(cfg, doc: dict, io, pr: dict | None = None) -> bool:
    """Do this exam's next piece of work; True when another may follow at once. `pr`, when
    given, was read after `doc`: a request made since stays wanted."""
    if _due(doc):
        _post_due(cfg, doc, io.github)
        return True
    if _answerable(doc) and _take_request(cfg, doc, io, pr):
        return True
    if _kind(doc):
        return _run_job(cfg, doc, io)
    return False


def _answerable(doc: dict) -> bool:
    """A request waits and no job or grading holds the exam."""
    return _wanted(doc) and not _busy(doc)


def _take_request(cfg, doc: dict, io, pr: dict | None) -> bool:
    """Answer a request with the PR as read now; True when a bypass settled it or an open
    or passed exam's head moved. Otherwise the exam's job, if any, runs as usual."""
    pr = pr or io.github.pr(doc["repo"], doc["pr"])
    if _bypass(cfg, doc, pr, io):
        return True
    return doc["state"] in ("open", "passed") and _reconcile(cfg, doc, pr["head_sha"])


def work(cfg, *, github, run, pr_exam, notify, now, usages, sweep: bool = False) -> None:
    """Do the pending work of every exam: settle its PR's merge or close (when requested,
    or for every exam in a sweep), post queued statuses, reconcile requests with
    the PR's head, and run each exam's jobs. Runs outside task capacity."""
    io = SimpleNamespace(github=github, run=run, pr_exam=pr_exam, notify=notify, now=now, usages=usages)
    for path in sorted(Path(cfg.state_dir, "exams").glob("*/*/*/state.json")):
        doc = json.loads(path.read_text())
        try:   # one exam's GitHub or script failure never stops another's work
            _work_exam(cfg, doc, io, sweep)
        except Exception as exc:  # noqa: BLE001 — the job was released; the next work retries
            print(f"exam work for {doc['repo']}#{doc['pr']} failed: {exc}", file=sys.stderr)


def _work_exam(cfg, doc: dict, io, sweep: bool) -> None:
    # one read of the PR serves the lifecycle and the request; `doc` stays as read
    # before it, so a request made during the read is reconciled on the next pass
    pr = io.github.pr(doc["repo"], doc["pr"]) if sweep or _wanted(doc) else None
    if pr and _lifecycle(cfg, doc, pr):
        return
    _steps(cfg, doc, io, pr)


def _steps(cfg, doc: dict, io, pr: dict | None) -> None:
    for _ in range(10):   # a pass is at most grade, status, reconcile, delta, status
        if not _step(cfg, doc, io, pr):
            return
        pr = None
        doc = read(cfg.state_dir, doc["repo"], doc["pr"])
