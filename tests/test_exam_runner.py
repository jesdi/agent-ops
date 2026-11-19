"""The production edges of exam generation: the claim race, the session's container
and the status token. The seam's own behaviour is in test_exam_generation.py."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from dispatcher import containers, exam_runner, exams
from tests.webfakes import make_config

NOW = datetime(2026, 11, 19, 23, 59, tzinfo=timezone.utc)


def test_a_delayed_worker_does_not_claim_an_exam_that_already_opened(tmp_path):
    cfg = make_config(tmp_path)
    stale = exams.request(cfg.state_dir, "o/r", 1)   # what the delayed worker read
    exams._update(cfg.state_dir, "o/r", 1, state="open", wanted=False, job=None)
    assert exams._claim(cfg, stale, "m", lambda: NOW) is False
    assert exams.read(cfg.state_dir, "o/r", 1)["job"] is None


def test_the_exam_container_has_no_token_and_no_clone(monkeypatch):
    cmd = containers.exam_cmd("exam-x", "/job", "/rec", "/skill-dir", "1g", "1",
                              "anthropic/claude-opus-5-5", "/job/prompt.md")
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert "/job:/job" in mounts and "/rec:/rec:ro" in mounts and "/skill-dir:/skill:ro" in mounts
    assert not any("/root/.config/gh" in m or ".gitconfig" in m for m in mounts)


def test_the_codex_exam_session_runs_outside_a_git_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    cmd = containers.exam_cmd("exam-x", "/job", "/rec", "/skill-dir", "1g", "1",
                              "openai/gpt-5", "/job/prompt.md")
    assert cmd[-1].startswith("codex exec ") and "--skip-git-repo-check" in cmd[-1]


def test_only_the_exam_token_holds_statuses_write(tmp_path):
    asked = []
    access = SimpleNamespace(grants=lambda repos, perms: asked.append(perms)
                             or {repos[0].lower(): SimpleNamespace(token="T")})
    calls = []
    run = lambda argv, **kw: calls.append(kw["env"]["GH_TOKEN"]) or SimpleNamespace(stdout="")
    exam_runner.Github(access, None, run=run).post_status("O/r", "abc", "exam", "pending", "x")
    assert asked == [exam_runner.PERMISSIONS] and calls == ["T"]
    assert exam_runner.PERMISSIONS["statuses"] == "write"
