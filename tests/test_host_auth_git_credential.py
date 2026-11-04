import subprocess

from dispatcher import host_auth, spec_publish


def test_publish_network_calls_use_installation_token_and_wait_on_outage(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(k.get("env")) or subprocess.CompletedProcess(a, 0, "", ""))
    host_auth._env.clear()
    host_auth._env["o/r"] = {"GH_TOKEN": "t", **host_auth._GIT_AS_GH}
    spec_publish._git("wt", "ls-remote", "origin", repo="O/R")
    assert seen[-1]["GH_TOKEN"] == "t" and seen[-1]["GIT_CONFIG_VALUE_1"] == "!gh auth git-credential"
    host_auth._env["o/r"] = host_auth.HostWait("down")
    seen.clear()
    r = spec_publish.ensure_published(worktree="wt", branch="b", repo="o/r", issue=1, artifact="specs/x/spec.md")
    assert "git invocation failed" in r.error
    host_auth._env.clear()
