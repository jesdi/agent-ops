"""Behavior tests for the box-variant git guardrail hook (ADR 0003 §1).

The hook is a Claude Code PreToolUse hook: JSON on stdin, exit 0 allows,
exit 2 blocks. Box sessions must push task branches (allowed) but never
force-push or touch main (blocked)."""
import json
import subprocess
from pathlib import Path

import pytest

HOOK = (Path(__file__).resolve().parent.parent
        / "provision" / "claude-home" / "hooks" / "block-dangerous-git.sh")


def run_hook(command):
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(["bash", str(HOOK)], input=payload,
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("cmd", [
    "git push origin task-12-fix",
    "git push -u origin HEAD",
    "git push",
    "git status",
    "git commit -m 'feat: x'",
    "git checkout -b feature/maintenance",   # 'main' inside a word: allowed
    "echo domain | grep main",               # non-git 'main': allowed
])
def test_allowed(cmd):
    r = run_hook(cmd)
    assert r.returncode == 0, f"{cmd!r} blocked: {r.stderr}"


@pytest.mark.parametrize("cmd", [
    "git push --force origin task-12-fix",
    "git push --force-with-lease",
    "git push -f",
    "git push -f origin feature",
    "git push origin main",
    "git push origin master",
    "git push origin HEAD:main",
    "git push upstream feature:master",
    "git reset --hard HEAD~1",
    "git clean -fd",
    "git clean -f",
    "git branch -D task-12-fix",
    "git checkout .",
    "git restore .",
])
def test_blocked(cmd):
    r = run_hook(cmd)
    assert r.returncode == 2, f"{cmd!r} not blocked"
    assert "BLOCKED" in r.stderr


def test_no_escape_hatch():
    # The mac hook honors an 'allow-dangerous' override; the box variant
    # must not — unattended sessions cannot be allowed to bypass the guard.
    r = run_hook("git push --force  # allow-dangerous")
    assert r.returncode == 2


def test_executable_bit():
    assert HOOK.stat().st_mode & 0o111, "hook must be executable"
