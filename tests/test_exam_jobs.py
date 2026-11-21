"""The exam job's claim, the runner the pass launches instead of doing exam work, and the
pinned `pr_exam.py` on the delta request the host writes."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dispatcher import exam_runner, exams
from tests.test_exam_generation import NOW, REPO
from tests.test_exam_pushes import NEW
from tests.webfakes import make_config


def test_a_delayed_worker_does_not_claim_an_exam_that_already_opened(tmp_path):
    cfg = make_config(tmp_path)
    stale = exams.request(cfg.state_dir, "o/r", 1)   # what the delayed worker read
    exams._update(cfg.state_dir, "o/r", 1, state="open", reconciled=1, job=None)
    job = {"kind": "generate", "head_sha": "a", "attempt": 0, "started": NOW.isoformat()}
    assert exams._claim(cfg, stale, job, lambda: NOW) is False
    assert exams.read(cfg.state_dir, "o/r", 1)["job"] is None


class Tabs:
    def __init__(self, alive=None):
        self.alive, self.ran = alive, []

    def find(self, label):
        return SimpleNamespace(alive=True) if self.alive else None

    def ensure(self, workspace, label, cwd, env=None):
        self.label, self.env = label, env
        return SimpleNamespace(run=lambda command: self.ran.append(command) or True)


@pytest.fixture
def tabs(monkeypatch):
    def use(alive=None):
        fake = Tabs(alive)
        monkeypatch.setattr(exam_runner.herdr.Tab, "find", fake.find)
        monkeypatch.setattr(exam_runner.herdr.Tab, "ensure", fake.ensure)
        return fake
    return use


def test_the_pass_launches_the_runner_in_its_tab_and_never_runs_a_session(tmp_path, tabs, monkeypatch):
    monkeypatch.setattr(exams, "work", lambda *a, **kw: pytest.fail("the pass ran exam work"))
    cfg = make_config(tmp_path)
    fake = tabs()
    exam_runner.launch(cfg, "targets.yaml")
    assert fake.ran == []   # no exam: nothing to launch
    exams.request(cfg.state_dir, "o/r", 1)
    exam_runner.launch(cfg, "targets.yaml")
    [command] = fake.ran
    assert fake.label == "exam-runner"
    assert "-m dispatcher.main --config targets.yaml --exam-run; exit" in command
    busy = tabs(alive=True)
    exam_runner.launch(cfg, "targets.yaml")
    assert busy.ran == []


def test_the_runner_tab_gets_the_wrapper_and_no_app_credential_in_herdr_s_argv(tmp_path, monkeypatch):
    """herdr's `tab create --env K=V` is argv, which `ps` shows every process on the box:
    the pane gets the wrapper its podman sessions start through, never the App's key."""
    from dispatcher import triage
    from tests.test_triage import _herdr_fake_creating
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----\nk\n")
    monkeypatch.setenv("AGENT_OPS_COMMAND_WRAPPER", "/infra/with-claude-token.sh")
    calls = []
    _herdr_fake_creating(monkeypatch, calls)
    cfg = make_config(tmp_path)
    exams.request(cfg.state_dir, "o/r", 1)
    exam_runner.launch(cfg, "targets.yaml")
    [create] = [c for c in calls if c[:2] == ["tab", "create"]]
    env = [create[i + 1] for i, a in enumerate(create) if a == "--env"]
    assert env == [f"{k}={v}" for k, v in triage._launch_env().items()] + [
        "AGENT_OPS_COMMAND_WRAPPER=/infra/with-claude-token.sh"]
    assert not any("GITHUB_APP" in a or "PRIVATE KEY" in a for call in calls for a in call)


def test_the_runner_mints_its_app_token_from_the_secret_cache_in_its_tab(tmp_path, monkeypatch):
    """The tab's env has no App field (the pane inherits the herdr server's, and none is
    forwarded): `--exam-run` decrypts the id and key from the cache files the units load."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import dispatcher.main as main
    from dispatcher import github_app
    from tests.test_github_app import APP_ID, FakeGitHub
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cache = tmp_path / "secrets"
    cache.mkdir()
    (cache / "GITHUB_APP_ID.cred").write_text(APP_ID + "\n")
    (cache / "GITHUB_APP_PRIVATE_KEY.cred").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    creds = tmp_path / "systemd-creds"   # cats the file when called as with-claude-token.sh does
    creds.write_text('#!/bin/sh\n[ "$1 $2 $3 $5" = "--user decrypt --name=$(basename "$4" .cred) -" ]'
                     ' || exit 1\nexec cat "$4"\n')
    creds.chmod(0o755)
    for field in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY"):
        monkeypatch.delenv(field, raising=False)
    monkeypatch.setenv("AGENT_OPS_CREDS", str(creds))
    monkeypatch.setenv("AGENT_OPS_SECRETS_DIR", str(cache))
    monkeypatch.setattr(github_app, "_http", FakeGitHub())
    cfg = make_config(tmp_path)
    monkeypatch.setattr(main, "_load", lambda path: (cfg, cfg))
    monkeypatch.setattr(main.sys, "argv", ["agent-ops-dispatcher", "--config", "t.yaml", "--exam-run"])
    ran = []
    monkeypatch.setattr(exam_runner, "run", lambda cfg_, deps: ran.append(deps))
    main.main()
    [deps] = ran
    inst = deps.app.installation("jesdi/analytics")
    assert deps.app.mint(inst, ["jesdi/analytics"], exam_runner.PERMISSIONS).token == "ghs_analytics"


def test_a_secret_cache_that_cannot_be_decrypted_reads_as_an_unregistered_app(tmp_path, monkeypatch):
    from dispatcher import github_app
    monkeypatch.setenv("AGENT_OPS_CREDS", str(tmp_path / "no-systemd-creds"))
    assert github_app.cached("GITHUB_APP_PRIVATE_KEY") == ""
    assert github_app.Client(secret=github_app.cached).registered() is False


def test_a_second_runner_exits_without_working(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    ran = []
    monkeypatch.setattr(exam_runner, "tick", lambda *a: ran.append(1))
    monkeypatch.setattr(exam_runner, "fetch_all", lambda cfg: {})
    from dispatcher.state import state_lock
    with state_lock(cfg.state_dir, exam_runner.RUN_LOCK):
        exam_runner.run(cfg, None)
    assert ran == []
    exam_runner.run(cfg, None)
    assert ran == [1]


SCRIPT = Path(os.environ.get("PR_EXAM_SCRIPT", exam_runner.SCRIPT))


@pytest.mark.skipif(not SCRIPT.is_file(), reason="the pinned pr-exam skill is not installed")
@pytest.mark.parametrize("concepts", [[], ["retry-budget"]])
def test_the_pinned_validator_accepts_the_delta_the_host_requests(tmp_path, concepts):
    """The production adapter and the real script on the host's delta request."""
    from dispatcher.exams import _spec
    d = tmp_path / "exams" / REPO / "240"
    job = d / "job"
    job.mkdir(parents=True)
    diff = "diff --git a/r.py b/r.py\n+retry(budget=3)\n"
    (job / "diff.patch").write_text(diff)
    (job / "body.md").write_text("")
    (d / "passed.diff").write_text("diff --git a/l.py b/l.py\n+lease()\n")
    doc = {"repo": REPO, "pr": 240, "passed_concepts": ["lease-ownership"], "followup": None}
    spec = _spec(SimpleNamespace(state_dir=tmp_path), doc, "delta", NEW, job, None, lambda: NOW, None)
    (tmp_path / "exam-records" / REPO).mkdir(parents=True)
    questions = [{"id": f"q{i}", "concept": c, "type": "choice", "prompt": f"Why {c}?",
                  "options": ["one", "two"]} for i, c in enumerate(concepts)]
    exam = {"version": 1, "mode": "delta", "repo": REPO, "pr": 240, "head_sha": NEW,
            "concepts": [{"id": c, "title": c, "kind": "concept"} for c in concepts],
            "questions": questions,
            "key": {q["id"]: {"answer": 0, "explanation": "Because.", "hunk": "+retry(budget=3)"}
                    for q in questions}}
    out, request = job / "exam.json", job / "request.json"
    out.write_text(json.dumps(exam))
    request.write_text(json.dumps(spec))
    adapter = exam_runner.PrExam(SCRIPT)
    assert adapter.validate(out, request) == []
    stale = {**exam, "concepts": [{"id": "lease-ownership", "title": "l", "kind": "concept"}]}
    out.write_text(json.dumps(stale))
    assert any("lease-ownership" in e for e in adapter.validate(out, request))
