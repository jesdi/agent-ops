"""Regressions for legacy ping authentication and per-identity history."""
import struct
import subprocess

from dispatcher import state, waitd
from web.sources import Sources

PEER_PID = 4242


class PeerConnection:
    """A socket whose kernel peer is PEER_PID."""

    def getsockopt(self, *args):
        return struct.pack("3i", PEER_PID, 0, 0)


def podman_top_owning(container):
    """`podman top <container> hpid` lists PEER_PID only for `container`."""
    calls = []

    def run(args, **kwargs):
        calls.append(args[2])
        if args[2] != container:
            raise subprocess.CalledProcessError(125, args)
        return subprocess.CompletedProcess(args, 0, f"HPID\n{PEER_PID}\n", "")

    return run, calls


def test_pre_rename_container_authenticates_its_stop_ping(tmp_path, monkeypatch):
    """A pre-rename task-12 container's unchanged hook sends target=""."""
    run, calls = podman_top_owning("task-12")
    monkeypatch.setattr(waitd.subprocess, "run", run)
    # Linux peer credentials, also on hosts (macOS) whose socket lacks them.
    monkeypatch.setattr(waitd.socket, "SO_PEERCRED", 17, raising=False)
    waitd.handle_ping(b'{"target":"","issue":12}', tmp_path,
                      legacy_sender=lambda target, issue: waitd._legacy_peer_matches(
                          PeerConnection(), target, issue))
    assert calls == ["task-12"]
    assert (tmp_path / "waiting-12").exists()


def test_named_legacy_ping_is_not_proven_by_pre_rename_container(tmp_path, monkeypatch):
    """The legacy name fallback does not let task-12 speak for task-shop-12."""
    run, calls = podman_top_owning("task-12")
    monkeypatch.setattr(waitd.subprocess, "run", run)
    # Linux peer credentials, also on hosts (macOS) whose socket lacks them.
    monkeypatch.setattr(waitd.socket, "SO_PEERCRED", 17, raising=False)
    waitd.handle_ping(b'{"target":"shop","issue":12}', tmp_path,
                      legacy_sender=lambda target, issue: waitd._legacy_peer_matches(
                          PeerConnection(), target, issue))
    assert calls == ["task-shop-12"]
    assert not state.has_waiting(tmp_path, "shop", 12)


class EveryIdentityLive:
    """Distinct, simultaneously live executions: output names its identity."""

    def is_alive(self, target, issue, repo=""):
        return True

    def capture_history(self, target, issue, lines=2000, repo=""):
        return f"history of {repo or 'unqualified'} {target}#{issue}"


def test_history_reads_only_its_own_repository_session(tmp_path):
    sources = Sources(str(tmp_path), EveryIdentityLive(), None)
    for repo in ("example/shop-frontend", "example/shop-backend"):
        assert sources.pane_history("shop", 12, repo=repo) == f"history of {repo} shop#12"
