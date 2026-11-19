"""Exam generation through `exams.work` with injected fakes, then the exam routes.

Interface under test (defined here, built by the ticket):
    exams.work(cfg, *, github, run, pr_exam, notify, now, usages)
    github.pr(repo, n) -> {"head_sha", "base_ref", "title", "body", "state", "merged", "labels"}
    github.label_events(repo, n) -> [{"event", "label", "actor"}], oldest first
    github.diff(repo, n) -> str
    github.file(repo, ref, path) -> str | None
    github.post_status(repo, sha, context, state, description)   (as the App)
    github.app() -> {"slug", "id"}
    run(request: Path, *, model: str, timeout: int) -> None   (the session writes `out`)
    pr_exam.validate(exam: Path, request: Path) -> list[str]   (errors, empty = valid)
    pr_exam.public(exam: Path) -> dict                         (the served projection)
    pr_exam.config(path: Path) -> {"errors", "enabled", "labels"}   (of the repo's .pr-exam.json)
    notify(text: str); now() -> aware datetime; usages: provider -> ProviderUsage
    exam_runner.PERMISSIONS: the only token scope that holds `statuses: write`.
"""
import dataclasses
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dispatcher import exams, host_auth, priority, task_tokens
from dispatcher.models import parse_policy
from tests.usagefakes import session_usage
from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources

CONSOLE = "https://box.tailnet.ts.net"
REPO = "jesdi/general-skills"
REQUEST = {"repo": REPO, "pr": 240}
GET = "/api/exam/jesdi/general-skills/240"
HEAD = "abc123"
NOW = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc)
DIFF = "diff --git a/lease.py b/lease.py\n+lease = acquire()\n+recheck(base_sha)\n"
BODY = "Adds lease ownership.\n```merge-risk v1\nrisk: low\n```\n"
GLOSSARY = "# Glossary\nLease: the right to merge.\n"
SECRETS = ("RUBRIC-PASS-TEXT", "RUBRIC-PARTIAL-TEXT", "EXPLANATION-TEXT", "HUNK-TEXT")


class Github:
    def __init__(self, glossary=GLOSSARY):
        self.glossary = glossary
        self.head = HEAD
        self.statuses, self.files = [], []
        self.labels, self.events = [], []   # the PR's labels now, and its label events
        self.configs = {}                   # ref -> the text of .pr-exam.json there

    def pr(self, repo, n):
        return {"head_sha": self.head, "base_ref": "main", "title": "Leases", "body": BODY,
                "state": "open", "merged": False, "labels": list(self.labels)}

    def label_events(self, repo, n):
        return list(self.events)

    def diff(self, repo, n):
        return DIFF

    def file(self, repo, ref, path):
        self.files.append((repo, ref, path))
        return self.configs.get(ref) if path == ".pr-exam.json" else self.glossary

    def post_status(self, repo, sha, context, state, description):
        self.statuses.append((repo, sha, context, state))

    def app(self):
        return {"slug": "agent-ops-hel1", "id": 12345}


def exam_doc(choices=3, texts=2):
    qs = [{"id": f"q{i}", "concept": f"c{i}", "type": "choice", "prompt": f"CHOICE-PROMPT-{i}",
           "options": [f"opt a{i}", f"opt b{i}"]} for i in range(choices)]
    qs += [{"id": f"t{i}", "concept": f"w{i}", "type": "text", "prompt": f"TEXT-PROMPT-{i}"}
           for i in range(texts)]
    key = {q["id"]: ({"answer": 1, "explanation": "EXPLANATION-TEXT", "hunk": "HUNK-TEXT"}
                     if q["type"] == "choice" else
                     {"rubric": {"pass": "RUBRIC-PASS-TEXT", "partial": "RUBRIC-PARTIAL-TEXT"},
                      "explanation": "EXPLANATION-TEXT", "hunk": "HUNK-TEXT"}) for q in qs}
    return {"version": 1, "mode": "first", "repo": REPO, "pr": 240, "head_sha": HEAD,
            "concepts": [{"id": q["concept"], "title": q["concept"], "kind": "why"} for q in qs],
            "questions": qs, "key": key}


class Session:
    """The injected `run=`: reads the request file, writes `out` as a model would."""

    def __init__(self, behaviour="ok"):
        self.behaviour, self.calls, self.requests = behaviour, [], []
        self.during = None

    def __call__(self, request, *, model, timeout):
        req = json.loads(Path(request).read_text())
        self.calls.append({"model": model, "timeout": timeout})
        self.requests.append({"request": req, "diff": Path(req["diff"]).read_text(),
                              "body": Path(req["body"]).read_text(),
                              "glossary": req["glossary"] and Path(req["glossary"]).read_text(),
                              "records_is_dir": Path(req["records"]).is_dir()})
        if self.during:
            self.during()
        if self.behaviour == "timeout":
            raise subprocess.TimeoutExpired("session", timeout)
        if self.behaviour != "nofile":
            exam = {**exam_doc(), "head_sha": req["head_sha"]}
            if self.behaviour == "invalid":
                exam["questions"] = []
            Path(req["out"]).write_text(json.dumps(exam))


class PrExam:
    """Fake of pr_exam.py: validate refuses an exam without questions, config reads the
    JSON file it is given."""

    def config(self, path):
        doc = json.loads(Path(path).read_text())
        errors = [f"unknown key: {k}" for k in doc if k not in ("enabled", "labels")]
        return {"errors": errors, "enabled": doc.get("enabled", True),
                "labels": doc.get("labels", {})}

    def validate(self, exam, request):
        return [] if json.loads(Path(exam).read_text())["questions"] else ["no questions: refused"]

    def public(self, exam):
        doc = json.loads(Path(exam).read_text())
        return {**{k: doc[k] for k in ("version", "repo", "pr", "head_sha")},
                "questions": [{k: q[k] for k in ("id", "type", "prompt", "options") if k in q}
                              for q in doc["questions"]]}


class Box:
    def __init__(self, tmp_path, monkeypatch, triage=("anthropic/claude-opus-5", "openai/gpt-5"),
                 usages=None, session=None):
        monkeypatch.setattr("web.sources.subprocess.run",
                            lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
        policy = parse_policy({"triage": list(triage), "untracked": "standard", "tracks": {
            "standard": {"when": "Everything.", **{s: ["anthropic/claude-opus-5"] for s in
                                                   ("spec", "plan", "implement", "review")}}}})
        self.cfg = dataclasses.replace(make_config(tmp_path / "state"), models=policy,
                                       console_url=CONSOLE)
        self.client = TestClient(create_app(self.cfg, Sources(self.cfg.state_dir, None, None,
                                                              clock=lambda: 1000.0)))
        self.github, self.session, self.notes = Github(), session or Session(), []
        self.usages = usages if usages is not None else {
            p: session_usage(provider=p, now=NOW) for p in ("anthropic", "openai")}

    def request(self):
        response = self.client.post("/api/exam", headers=HEADERS, json=REQUEST)
        assert response.status_code == 200
        return response.json()

    def work(self):
        exams.work(self.cfg, github=self.github, run=self.session, pr_exam=PrExam(),
                   notify=self.notes.append, now=lambda: NOW, usages=self.usages)

    def read(self):
        return self.client.get(GET, headers=HEADERS)

    def posted(self):
        return [s for s in self.github.statuses if s[2] == "exam"]


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def test_work_marks_the_head_pending_runs_one_session_and_opens_the_exam(box):
    box.request()
    box.work()
    assert (REPO, HEAD, "exam", "pending") in box.posted()
    assert not any(s[3] == "success" for s in box.posted())
    assert len(box.session.calls) == 1
    body = box.read().json()
    assert body["state"] == "open"
    assert body["head_sha"] == HEAD


def test_the_request_file_names_diff_body_glossary_records_and_utc_date(box):
    box.request()
    box.work()
    [seen] = box.session.requests
    req = seen["request"]
    assert (seen["diff"], seen["body"], seen["glossary"]) == (DIFF, BODY, GLOSSARY)
    assert [f for f in box.github.files if f[2] == "GLOSSARY.md"] == [(REPO, "main", "GLOSSARY.md")]
    assert {(repo, ref) for repo, ref, _ in box.github.files} == {(REPO, "main")}
    assert req["today"] == "2026-11-19"
    assert seen["records_is_dir"]
    assert req["records"] == str(Path(box.cfg.state_dir) / "exam-records" / REPO)
    assert (req["mode"], req["repo"], req["pr"], req["head_sha"]) == ("first", REPO, 240, HEAD)
    assert set(req) == {"mode", "repo", "pr", "head_sha", "diff", "body", "glossary", "records",
                        "today", "only_concepts", "prior_concepts", "prior_prompts", "out"}


def test_glossary_is_null_when_the_repo_has_none(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch)
    box.github.glossary = None
    box.request()
    box.work()
    assert box.session.requests[0]["request"]["glossary"] is None


def test_repeating_work_or_the_request_runs_no_second_session(box):
    box.request()
    box.work()
    box.work()
    box.request()
    box.work()
    assert len(box.session.calls) == 1


def test_work_called_while_the_job_is_recorded_starts_no_second_session(box):
    box.session.during = box.work
    box.request()
    box.work()
    assert len(box.session.calls) == 1
    assert box.read().json()["state"] == "open"


def test_the_page_serves_the_five_questions_and_no_answer_material(box):
    box.request()
    box.work()
    response = box.read()
    body = response.json()
    served = body["exam"]["questions"]
    assert [q["prompt"] for q in served] == [
        "CHOICE-PROMPT-0", "CHOICE-PROMPT-1", "CHOICE-PROMPT-2", "TEXT-PROMPT-0", "TEXT-PROMPT-1"]
    assert served[0]["options"] == ["opt a0", "opt b0"]
    for secret in SECRETS:
        assert secret not in response.text
    for word in ("rubric", "explanation", "hunk", "answer", "key"):
        assert word not in json.dumps(body).lower().replace("attempt", "")


@pytest.mark.parametrize("behaviour", ["invalid", "nofile", "timeout"])
def test_a_failed_job_sets_error_keeps_exam_pending_and_the_next_request_retries(box, behaviour):
    box.session.behaviour = behaviour
    box.request()
    box.work()
    body = box.read().json()
    assert body["state"] == "error"
    assert body["detail"]
    if behaviour == "timeout":
        assert box.session.calls[0]["timeout"] == 1200
    assert box.posted() == [(REPO, HEAD, "exam", "pending")]
    box.session.behaviour = "ok"
    box.request()
    box.work()
    assert len(box.session.calls) == 2
    assert box.read().json()["state"] == "open"
    assert not any(s[3] == "success" for s in box.posted())


def test_an_invalid_exam_names_the_validator_reason(box):
    box.session.behaviour = "invalid"
    box.request()
    box.work()
    assert "no questions: refused" in box.read().json()["detail"]


def test_the_session_model_is_the_first_admitted_triage_entry_in_priority_order(box):
    priority.save(box.cfg.state_dir, "openai", actor="t", now=NOW)
    box.request()
    box.work()
    assert box.session.calls[0]["model"] == "openai/gpt-5"


def test_an_unadmitted_provider_is_skipped(tmp_path, monkeypatch):
    usages = {"anthropic": session_usage(provider="anthropic", now=NOW),
              "openai": session_usage(util=1.0, provider="openai", now=NOW)}
    box = Box(tmp_path, monkeypatch, usages=usages)
    priority.save(box.cfg.state_dir, "openai", actor="t", now=NOW)
    box.request()
    box.work()
    assert box.session.calls[0]["model"] == "anthropic/claude-opus-5"


def test_when_no_provider_admits_no_session_starts_and_the_exam_stays_generating(tmp_path, monkeypatch):
    usages = {p: session_usage(util=1.0, provider=p, now=NOW) for p in ("anthropic", "openai")}
    box = Box(tmp_path, monkeypatch, usages=usages)
    box.request()
    box.work()
    assert box.session.calls == []
    assert box.read().json()["state"] == "generating"
    box.usages = {p: session_usage(provider=p, now=NOW) for p in ("anthropic", "openai")}
    box.work()
    assert len(box.session.calls) == 1
    assert box.read().json()["state"] == "open"


def test_a_result_for_a_head_that_moved_is_dropped_and_the_new_head_is_generated(box):
    box.session.during = lambda: setattr(box.github, "head", "def456")
    box.request()
    box.work()
    body = box.read().json()
    assert body["state"] != "open"
    assert body.get("head_sha") != HEAD
    assert "exam" not in body or not body["exam"]
    assert not any(s[3] == "success" for s in box.posted())
    box.session.during = None
    box.work()
    assert [r["request"]["head_sha"] for r in box.session.requests] == [HEAD, "def456"]
    body = box.read().json()
    assert body["state"] == "open"
    assert body["head_sha"] == "def456"
    assert (REPO, "def456", "exam", "pending") in box.posted()


def test_a_full_task_capacity_does_not_stop_an_exam_job(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch)
    state = Path(box.cfg.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    for i in range(box.cfg.capacity):
        (state / f"task-other-{i}.json").write_text(json.dumps({"state": "running"}))
    box.request()
    box.work()
    assert len(box.session.calls) == 1
    assert box.read().json()["state"] == "open"


def test_only_the_exam_runner_holds_statuses_write(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dispatcher import exam_runner

    monkeypatch.setenv("GH_TOKEN", "PERSONAL-TOKEN")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    minted, posted = [], []

    def grants(repos, perms, **kwargs):
        credential = f"APP-TOKEN-{len(minted)}"
        minted.append((list(repos), dict(perms), credential))
        return {r.lower(): SimpleNamespace(token=credential, expires_at=NOW) for r in repos}

    access = SimpleNamespace(grants=grants)
    host = host_auth.begin([[REPO]], access)
    assert host.env(REPO)["GH_TOKEN"] == minted[0][2]
    task_dir = task_tokens.issue(access, tmp_path, "task", REPO)
    assert minted[1][2] in (Path(task_dir) / "hosts.yml").read_text()
    with task_tokens.triage(access, tmp_path, REPO) as triage_dir:
        assert minted[2][2] in (Path(triage_dir) / "hosts.yml").read_text()

    def post(argv, **kwargs):
        posted.append((argv, kwargs["env"]["GH_TOKEN"]))
        return SimpleNamespace(stdout="{}")

    exam_runner.Github(access, None, run=post).post_status(
        REPO, HEAD, "exam", "pending", "Generating exam")
    assert len(minted) == 4
    assert all(repos == [REPO] for repos, _, _ in minted)
    assert [perms.get("statuses", "read") for _, perms, _ in minted] == [
        "read", "read", "read", "write"]
    [(argv, credential)] = posted
    assert credential == minted[3][2]
    assert argv[:2] == ["gh", "api"]
    assert "POST" in argv and f"repos/{REPO}/statuses/{HEAD}" in argv
    assert "context=exam" in argv and "state=pending" in argv

    assert exam_runner.PERMISSIONS["statuses"] == "write"
    assert host_auth.PERMISSIONS["statuses"] == "read"
    assert task_tokens.TASK_PERMISSIONS.get("statuses") != "write"
    assert task_tokens.TRIAGE_PERMISSIONS.get("statuses") != "write"


def test_the_status_is_posted_with_the_exam_token_that_alone_holds_statuses_write():
    from types import SimpleNamespace

    from dispatcher import exam_runner

    def grants(repos, perms):
        tok = "WRITE-TOKEN" if perms.get("statuses") == "write" else "READ-TOKEN"
        return {repos[0].lower(): SimpleNamespace(token=tok)}

    used = []
    run = lambda argv, **kw: used.append((argv, kw["env"]["GH_TOKEN"])) or SimpleNamespace(stdout="{}")
    gh = exam_runner.Github(SimpleNamespace(grants=grants), None, run=run)
    gh.post_status("Jesdi/r", "abc", "exam", "pending", "x")
    [(argv, token)] = used
    assert "repos/Jesdi/r/statuses/abc" in " ".join(argv)
    assert token == "WRITE-TOKEN"


def _session_cmd(tmp_path, model):
    from types import SimpleNamespace

    from dispatcher import exam_runner

    job = tmp_path / "state" / "exams" / "o__r" / "1"
    job.mkdir(parents=True)
    request = job / "request.json"
    request.write_text(json.dumps({"records": "/records"}))
    cmds = []
    cfg = SimpleNamespace(session_memory="1g", session_cpus="1")
    exam_runner.session(cfg, run=lambda argv, **kw: cmds.append(argv))(
        request, model=model, timeout=60)
    [cmd] = cmds
    return cmd, str(job)


@pytest.mark.parametrize("model", ["anthropic/claude-opus-5", "openai/gpt-5"])
def test_the_session_container_mounts_no_credentials_and_no_clone(tmp_path, model):
    cmd, job = _session_cmd(tmp_path, model)
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    envs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert f"{job}:{job}" in mounts
    assert "/records:/records:ro" in mounts
    assert any(m.endswith(":/skill:ro") for m in mounts)
    assert cmd[cmd.index("-w") + 1] == job
    assert not any(".config/gh" in m or ".gitconfig" in m or "clone" in m for m in mounts)
    assert not any(e.upper().startswith(("GH_", "GITHUB_")) for e in envs)


def test_host_route_is_503_before_work_and_serves_the_app_after(box):
    assert box.client.get("/api/exam/host", headers=HEADERS).status_code == 503
    box.request()
    box.work()
    response = box.client.get("/api/exam/host", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"app_slug": "agent-ops-hel1", "app_id": 12345}
