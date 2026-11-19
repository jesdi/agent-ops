"""Exam state on the box: `<state_dir>/exams/<owner>/<name>/<pr>/state.json`.

Every write holds the exams lock and renames atomically."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import priority
from dispatcher.config import routed_providers
from dispatcher.models import triage_entry
from dispatcher.state import state_lock, write_json_atomic
from dispatcher.usage import admits

LOCK = "exams.lock"
TIMEOUT = 1200   # a generation session runs 20 minutes at most
APP = "exam-app.json"


def _path(state_dir: str | Path, repo: str, pr: int) -> Path:
    return Path(state_dir) / "exams" / repo / str(pr) / "state.json"


def read(state_dir: str | Path, repo: str, pr: int) -> dict | None:
    try:
        return json.loads(_path(state_dir, repo, pr).read_text())
    except FileNotFoundError:
        return None


def request(state_dir: str | Path, repo: str, pr: int) -> dict:
    """Record that this PR's exam is wanted; a repeat finds the same state."""
    with state_lock(state_dir, LOCK):
        existing = read(state_dir, repo, pr)
        if existing is not None:
            if existing["state"] == "error" and not existing.get("grading"):   # a request after a failure retries
                existing.update(state="new", wanted=True)
                write_json_atomic(_path(state_dir, repo, pr), existing, indent=2)
            elif existing["state"] in ("open", "passed", "grading", "generating") and not existing["wanted"]:
                existing["wanted"] = True   # work() compares the head and clears it; kept through grading and generation
                write_json_atomic(_path(state_dir, repo, pr), existing, indent=2)
            return existing
        doc = {"repo": repo, "pr": pr, "state": "new", "head_sha": None, "debt": False,
               "detail": "", "attempt": 0, "wanted": True, "job": None, "status": None,
               "merged": False, "passed_concepts": [], "grading": None, "results": []}
        write_json_atomic(_path(state_dir, repo, pr), doc, indent=2)
        return doc


def _dir(state_dir, repo: str, pr: int) -> Path:
    return _path(state_dir, repo, pr).parent


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


def _update(state_dir, repo: str, pr: int, **fields) -> dict:
    with state_lock(state_dir, LOCK):
        doc = read(state_dir, repo, pr)
        doc.update(fields)
        write_json_atomic(_path(state_dir, repo, pr), doc, indent=2)
        return doc


def _claim(cfg, doc: dict, model: str, now) -> bool:
    """One job per exam: False when a live job is recorded."""
    with state_lock(cfg.state_dir, LOCK):
        doc = read(cfg.state_dir, doc["repo"], doc["pr"])
        job = doc["job"]
        if doc["state"] != "generating" or not (doc["wanted"] or job):
            return False   # completed, failed or moved on since the caller read it
        if job and now() - datetime.fromisoformat(job["started"]) < timedelta(seconds=TIMEOUT + 300):
            return False
        doc.update(job={"started": now().isoformat(), "model": model}, wanted=False)
        write_json_atomic(_path(cfg.state_dir, doc["repo"], doc["pr"]), doc, indent=2)
        return True


def _generate(cfg, doc, pr, model, *, github, run, pr_exam, now,
              only=None, prior=None, delta=None) -> tuple[str, str]:
    """Run one session; (state, detail). `only` makes it a follow-up on those concepts,
    `prior` is the exam it follows, `delta` (the passed concepts) makes it the delta judgment
    after a pass: "passed" is returned when the push added no concept."""
    repo, n = doc["repo"], doc["pr"]
    job = _dir(cfg.state_dir, repo, n) / "job"
    shutil.rmtree(job, ignore_errors=True)
    job.mkdir(parents=True)
    records = Path(cfg.state_dir) / "exam-records" / repo
    records.mkdir(parents=True, exist_ok=True)
    glossary = github.file(repo, pr["base_ref"], "GLOSSARY.md")
    diff = github.diff(repo, n)
    (job / "diff.patch").write_text(diff)
    (_dir(cfg.state_dir, repo, n) / "head.diff").write_text(diff)   # outside the job: the session cannot swap it
    (job / "body.md").write_text(pr["body"] or "")
    if glossary is not None:
        (job / "GLOSSARY.md").write_text(glossary)
    out, request = job / "exam.json", job / "request.json"
    spec = {
        "mode": "delta" if delta is not None else "followup" if only else "first", "repo": repo, "pr": n, "head_sha": pr["head_sha"],
        "diff": str(job / "diff.patch"), "body": str(job / "body.md"),
        "glossary": None if glossary is None else str(job / "GLOSSARY.md"),
        "records": str(records), "today": now().astimezone(timezone.utc).date().isoformat(),
        "only_concepts": only, "out": str(out),
        "prior_concepts": [c["id"] for c in (prior or {}).get("concepts", [])],
        "prior_prompts": [q["prompt"] for q in (prior or {}).get("questions", [])]}
    if delta is not None:
        shutil.copyfile(_dir(cfg.state_dir, repo, n) / "passed.diff", job / "passed.diff")
        spec.update(passed_diff=str(job / "passed.diff"), passed_concepts=delta)
    request.write_text(json.dumps(spec))
    try:
        run(request, model=model, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "error", f"the session ran past {TIMEOUT // 60} minutes"
    except (subprocess.SubprocessError, OSError) as exc:
        return "error", f"the session failed: {exc}"
    if not out.exists():
        return "error", "the session wrote no exam file"
    if out.is_symlink() or not out.is_file():   # the job dir is writable by the session
        return "error", "the session's exam file is not a regular file"
    request.unlink(missing_ok=True)   # a symlink here must not take our write through it
    request.write_text(json.dumps(spec))   # validate against our request, not a rewritten one
    if errors := pr_exam.validate(out, request):
        return "error", "; ".join(errors)
    if delta is not None and not json.loads(out.read_text())["questions"]:
        return "passed", ""   # no new concept
    write_json_atomic(_dir(cfg.state_dir, repo, n) / "public.json", pr_exam.public(out))
    shutil.copyfile(out, _dir(cfg.state_dir, repo, n) / "exam.json")   # the key, out of the session's reach
    return "open", ""


def _entry(cfg, usages, when):
    return triage_entry(
        cfg.models, lambda m: admits(usages, m, when, cfg.pace).admitted,
        priority.order(priority.load(cfg.state_dir, routed_providers(cfg)),
                       usages, when, cfg.pace))


def submit(state_dir, repo: str, pr: int, attempt: int, head_sha: str, answers: dict) -> str:
    """Accept the answers of the open attempt: "ok", "conflict" (repeated, earlier or
    another head) or "incomplete". Changes nothing unless "ok"."""
    with state_lock(state_dir, LOCK):
        doc = read(state_dir, repo, pr)
        if (doc is None or doc["state"] != "open" or doc["attempt"] != attempt
                or doc["head_sha"] != head_sha):
            return "conflict"
        questions = public(state_dir, repo, pr)["questions"]
        for q in questions:
            a = answers.get(q["id"])
            if isinstance(a, bool) or isinstance(a, int) != (q["type"] == "choice") \
                    or not isinstance(a, (int, str)):
                return "incomplete"
        write_json_atomic(_dir(state_dir, repo, pr) / "answers.json", {q["id"]: answers[q["id"]] for q in questions})
        doc.update(state="grading", grading={"attempt": attempt})
        write_json_atomic(_path(state_dir, repo, pr), doc, indent=2)
        return "ok"


def _grade(cfg, doc, sha, model, *, github, run) -> tuple[dict | None, str]:
    """Grades of the free-text answers by a fresh session; (grades, error)."""
    d = _dir(cfg.state_dir, doc["repo"], doc["pr"])
    key = json.loads((d / "exam.json").read_text())
    answers = json.loads((d / "answers.json").read_text())
    texts = [q for q in key["questions"] if q["type"] == "text"]
    if not texts:
        return {}, ""
    job = d / "job"
    shutil.rmtree(job, ignore_errors=True)
    job.mkdir(parents=True)
    (job / "diff.patch").write_text(github.diff(doc["repo"], doc["pr"]))
    (job / "questions.json").write_text(json.dumps([
        {"id": q["id"], "prompt": q["prompt"], "rubric": key["key"][q["id"]]["rubric"]} for q in texts]))
    (job / "answers.json").write_text(json.dumps({q["id"]: answers[q["id"]] for q in texts}))
    out, request = job / "grades.json", job / "request.json"
    request.write_text(json.dumps({
        "mode": "grade", "repo": doc["repo"], "pr": doc["pr"], "head_sha": sha,
        "diff": str(job / "diff.patch"), "questions": str(job / "questions.json"),
        "answers": str(job / "answers.json"), "out": str(out)}))
    try:
        run(request, model=model, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"the grading session ran past {TIMEOUT // 60} minutes"
    except (subprocess.SubprocessError, OSError) as exc:
        return None, f"the grading session failed: {exc}"
    if out.is_symlink() or not out.is_file():
        return None, "the grading session wrote no regular grades file"
    try:
        grades = json.loads(out.read_text())
        assert all(grades[q["id"]]["grade"] in ("pass", "partial", "fail") for q in texts)
    except (ValueError, KeyError, TypeError, AssertionError):
        return None, "the grading session's grades are malformed"
    return grades, ""


def _record(cfg, d, repo, n, *, pr_exam, now) -> bool:
    """Write the graded attempt's learning records; False (state=error) when recording failed."""
    records = Path(cfg.state_dir) / "exam-records" / repo
    try:
        with state_lock(cfg.state_dir, "exam-records.lock"):   # numbering is read-then-write
            pr_exam.record(records, d / "exam.json", d / "result.json",
                           now().astimezone(timezone.utc).date().isoformat())
    except (ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        _update(cfg.state_dir, repo, n, state="error", detail=f"recording failed: {exc}")
        return False
    return True


def _finish(cfg, doc, pr, sha, model, *, github, run, pr_exam, now) -> bool:
    """Grade the submitted attempt and settle it; False when the head moved, so the caller replaces the exam."""
    repo, n = doc["repo"], doc["pr"]
    d = _dir(cfg.state_dir, repo, n)
    _update(cfg.state_dir, repo, n, state="grading", detail="")
    grades, error = _grade(cfg, doc, sha, model, github=github, run=run)
    diff = github.diff(repo, n)   # taken before the head check: it is the diff of `sha` when the check holds
    if github.pr(repo, n)["head_sha"] != sha:
        return False
    if grades is None:
        _update(cfg.state_dir, repo, n, state="error", detail=error)
        return True
    (d / "grades.json").write_text(json.dumps(grades))
    try:
        scored = pr_exam.score(d / "exam.json", d / "answers.json", d / "grades.json")
    except (ValueError, KeyError, subprocess.SubprocessError, OSError) as exc:
        _update(cfg.state_dir, repo, n, state="error", detail=f"scoring failed: {exc}")
        return True
    (d / "result.json").write_text(json.dumps(scored))
    result = {"attempt": doc["attempt"], "passed": scored["passed"], "score": scored["score"],
              "max": scored["max"], "questions": [
                  {k: v for k, v in q.items() if k in ("id", "concept", "points")
                   or (q["points"] < 1 and k in ("explanation", "hunk"))} for q in scored["questions"]]}
    exam = json.loads((d / "exam.json").read_text())
    missed = set() if scored["passed"] else set(scored["missed"])   # missed concepts return in the follow-up
    fields = {"grading": None, "results": [*doc.get("results", []), result],
              "passed_concepts": list(dict.fromkeys(
                  [*doc["passed_concepts"], *(c["id"] for c in exam["concepts"] if c["id"] not in missed)]))}
    if scored["passed"]:
        if not _record(cfg, d, repo, n, pr_exam=pr_exam, now=now):
            return True
        (d / "passed.diff").write_text(diff)
        github.post_status(repo, sha, "exam", "success", "Exam passed")
        _update(cfg.state_dir, repo, n, state="passed", status={"sha": sha, "state": "success"}, **fields)
        return True
    if not _record(cfg, d, repo, n, pr_exam=pr_exam, now=now):
        return True   # before the follow-up, which reads these records and replaces exam.json
    state, detail = _generate(cfg, doc, github.pr(repo, n), model, github=github, run=run,
                              pr_exam=pr_exam, now=now, only=list(dict.fromkeys(scored["missed"])),
                              prior=exam)
    if github.pr(repo, n)["head_sha"] != sha:   # the exam on the old head is replaced, attempts and all
        return False
    _update(cfg.state_dir, repo, n, state=state, detail=detail, attempt=doc["attempt"] + 1, **fields)
    return True


def _reconcile(cfg, doc, github) -> dict | None:
    """A request for an open or passed exam: None when the head is unchanged, else the exam
    sent back to generating (an open one is dropped, a passed one gets a delta judgment)."""
    repo, n = doc["repo"], doc["pr"]
    if github.pr(repo, n)["head_sha"] == doc["head_sha"]:
        _update(cfg.state_dir, repo, n, wanted=False)
        return None
    if doc["state"] == "passed":
        return _update(cfg.state_dir, repo, n, state="generating", delta=True, job=None, wanted=True)
    return _replace(cfg, repo, n)


def _replace(cfg, repo, n) -> dict:
    """Drop every attempt of an exam whose head moved and send it back to generating."""
    d = _dir(cfg.state_dir, repo, n)
    for name in ("public.json", "exam.json", "answers.json", "grades.json", "result.json", "passed.diff"):
        (d / name).unlink(missing_ok=True)
    shutil.rmtree(d / "job", ignore_errors=True)
    return _update(cfg.state_dir, repo, n, state="generating", head_sha=None, attempt=0, results=[],
                   passed_concepts=[], status=None, grading=None, detail="", job=None, wanted=True)


def work(cfg, *, github, run, pr_exam, notify, now, usages) -> None:
    """Generate every wanted exam: the head goes pending as the App, one session
    runs, the questions are published. Runs outside task capacity."""
    for path in sorted(Path(cfg.state_dir, "exams").glob("*/*/*/state.json")):
        doc = json.loads(path.read_text())
        if doc.get("grading"):
            entry = _entry(cfg, usages, now())
            if entry is None:
                continue
            sha = doc["head_sha"]
            if not _finish(
                    cfg, doc, github.pr(doc["repo"], doc["pr"]), sha, entry.model_id,
                    github=github, run=run, pr_exam=pr_exam, now=now):
                _replace(cfg, doc["repo"], doc["pr"])
            continue
        if doc["state"] in ("open", "passed") and doc["wanted"] and (doc := _reconcile(cfg, doc, github)) is None:
            continue
        if doc["state"] == "error" or not (doc["wanted"] or doc["job"]):
            continue
        repo, n = doc["repo"], doc["pr"]
        if doc["state"] == "new":
            doc = _update(cfg.state_dir, repo, n, state="generating")
        entry = _entry(cfg, usages, now())
        if entry is None or not _claim(cfg, doc, entry.model_id, now):
            continue
        pr = github.pr(repo, n)
        sha = pr["head_sha"]
        with state_lock(cfg.state_dir, LOCK):   # app() is cheap, but write only on change
            write_json_atomic(Path(cfg.state_dir) / APP, {
                "app_slug": (info := github.app())["slug"], "app_id": info["id"]})
        if doc["status"] != {"sha": sha, "state": "pending"}:
            github.post_status(repo, sha, "exam", "pending", "Exam in progress")
            doc = _update(cfg.state_dir, repo, n, status={"sha": sha, "state": "pending"})
        delta = doc["passed_concepts"] if doc.get("delta") else None
        state, detail = _generate(cfg, doc, pr, entry.model_id, github=github, run=run,
                                  pr_exam=pr_exam, now=now, delta=delta)
        if github.pr(repo, n)["head_sha"] != sha:   # the head moved: drop this result
            _update(cfg.state_dir, repo, n, job=None, wanted=True, state="generating")
            continue
        if state == "passed":   # the delta found no new concept: the push is covered
            d = _dir(cfg.state_dir, repo, n)
            shutil.copyfile(d / "head.diff", d / "passed.diff")
            github.post_status(repo, sha, "exam", "success", "Exam passed")
            _update(cfg.state_dir, repo, n, state="passed", detail="", job=None, delta=False,
                    head_sha=sha, status={"sha": sha, "state": "success"})
            continue
        _update(cfg.state_dir, repo, n, state=state, detail=detail, job=None,
                head_sha=sha if state == "open" else doc["head_sha"],
                attempt=doc["attempt"] + 1,
                **({"delta": False, "results": []} if delta is not None and state == "open" else {}))
        if state == "open" and cfg.console_url:
            notify(f"Exam ready: {cfg.console_url}/exam/{repo}/{n}")
