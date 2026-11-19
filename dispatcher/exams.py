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
            if existing["state"] == "error":   # a request after a failure retries
                existing.update(state="new", wanted=True)
                write_json_atomic(_path(state_dir, repo, pr), existing, indent=2)
            return existing
        doc = {"repo": repo, "pr": pr, "state": "new", "head_sha": None, "debt": False,
               "detail": "", "attempt": 0, "wanted": True, "job": None, "status": None,
               "merged": False, "passed_concepts": []}
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


def _generate(cfg, doc, pr, model, *, github, run, pr_exam, now) -> tuple[str, str]:
    """Run one session; (state, detail)."""
    repo, n = doc["repo"], doc["pr"]
    job = _dir(cfg.state_dir, repo, n) / "job"
    shutil.rmtree(job, ignore_errors=True)
    job.mkdir(parents=True)
    records = Path(cfg.state_dir) / "exam-records" / repo
    records.mkdir(parents=True, exist_ok=True)
    glossary = github.file(repo, pr["base_ref"], "GLOSSARY.md")
    (job / "diff.patch").write_text(github.diff(repo, n))
    (job / "body.md").write_text(pr["body"] or "")
    if glossary is not None:
        (job / "GLOSSARY.md").write_text(glossary)
    out, request = job / "exam.json", job / "request.json"
    spec = {
        "mode": "first", "repo": repo, "pr": n, "head_sha": pr["head_sha"],
        "diff": str(job / "diff.patch"), "body": str(job / "body.md"),
        "glossary": None if glossary is None else str(job / "GLOSSARY.md"),
        "records": str(records), "today": now().astimezone(timezone.utc).date().isoformat(),
        "only_concepts": None, "prior_concepts": [], "prior_prompts": [], "out": str(out)}
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
    write_json_atomic(_dir(cfg.state_dir, repo, n) / "public.json", pr_exam.public(out))
    return "open", ""


def work(cfg, *, github, run, pr_exam, notify, now, usages) -> None:
    """Generate every wanted exam: the head goes pending as the App, one session
    runs, the questions are published. Runs outside task capacity."""
    for path in sorted(Path(cfg.state_dir, "exams").glob("*/*/*/state.json")):
        doc = json.loads(path.read_text())
        if doc["state"] == "error" or not (doc["wanted"] or doc["job"]):
            continue
        repo, n = doc["repo"], doc["pr"]
        if doc["state"] == "new":
            doc = _update(cfg.state_dir, repo, n, state="generating")
        when = now()
        entry = triage_entry(
            cfg.models, lambda m: admits(usages, m, when, cfg.pace).admitted,
            priority.order(priority.load(cfg.state_dir, routed_providers(cfg)),
                           usages, when, cfg.pace))
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
        state, detail = _generate(cfg, doc, pr, entry.model_id, github=github, run=run,
                                  pr_exam=pr_exam, now=now)
        if github.pr(repo, n)["head_sha"] != sha:   # the head moved: drop this result
            _update(cfg.state_dir, repo, n, job=None, wanted=True, state="generating")
            continue
        _update(cfg.state_dir, repo, n, state=state, detail=detail, job=None,
                head_sha=sha if state == "open" else doc["head_sha"],
                attempt=doc["attempt"] + 1)
        if state == "open" and cfg.console_url:
            notify(f"Exam ready: {cfg.console_url}/exam/{repo}/{n}")
