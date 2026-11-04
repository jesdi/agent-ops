import subprocess

from dispatcher import host_auth, spec_publish


def test_publish_network_calls_use_installation_token_and_wait_on_outage(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(k.get("env")) or subprocess.CompletedProcess(a, 0, "", ""))
    auth = host_auth.HostAuth(tokens={"o/r": "t"})
    spec_publish._git("wt", "ls-remote", "origin", env=auth.env("O/R"))
    assert seen[-1]["GH_TOKEN"] == "t" and seen[-1]["GIT_CONFIG_VALUE_1"] == "!gh auth git-credential"
    seen.clear()
    r = spec_publish.ensure_published(worktree="wt", branch="b", repo="o/r", issue=1,
                                      artifact="specs/x/spec.md",
                                      auth=host_auth.HostAuth(waits={"o/r": "down"}))
    assert "waits for the GitHub App" in r.error and seen == []


def test_host_wait_is_not_an_os_error():
    """A handler of a failed call (`except OSError`) must never read an App outage as one."""
    assert not issubclass(host_auth.HostWait, OSError)


def test_without_begin_every_repository_is_on_the_stored_auth():
    assert host_auth.STORED.env("o/r") is None and host_auth.STORED.kw("o/r") == {}
