"""Startup sweep: a fresh web process has zero terminals by definition, so
any attached-<N> marker or grouped view-* tmux session is a leak from a hard
kill (SIGKILL/OOM — expected on a 4 GB box, ADR 0002).  A leftover marker
wedges the task permanently: _drive_task returns immediately every pass."""
import subprocess

from dispatcher import state
from web.__main__ import sweep_stale_terminals


class FakeRun:
    """Records tmux invocations and replays canned stdout."""

    def __init__(self, sessions=(), fail=False):
        self.sessions = list(sessions)
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if self.fail:
            raise OSError("tmux missing")
        if "list-sessions" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="\n".join(self.sessions) + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_clears_every_attached_marker(tmp_path):
    for issue in (7, 12):
        state.mark_attached(tmp_path, "alpha", issue)
    assert state.has_attached(tmp_path, "alpha", 7)

    sweep_stale_terminals(tmp_path, run=FakeRun())

    assert not state.has_attached(tmp_path, "alpha", 7)
    assert not state.has_attached(tmp_path, "alpha", 12)


def test_kills_orphaned_view_sessions_only(tmp_path):
    run = FakeRun(sessions=["task-7", "view-abc123", "view-deadbe", "misc"])

    sweep_stale_terminals(tmp_path, run=run)

    killed = [c[0][-1] for c in run.calls if "kill-session" in c[0]]
    assert killed == ["view-abc123", "view-deadbe"]
    # every tmux call is time-bounded: a wedged tmux server must not hang
    # startup forever
    assert all(c[1].get("timeout") for c in run.calls)


def test_survives_a_missing_or_wedged_tmux(tmp_path):
    state.mark_attached(tmp_path, "alpha", 7)

    sweep_stale_terminals(tmp_path, run=FakeRun(fail=True))  # must not raise

    # marker clearing does not depend on tmux being reachable
    assert not state.has_attached(tmp_path, "alpha", 7)


def test_ignores_unrelated_state_files(tmp_path):
    (tmp_path / "attached-not-a-number").write_text("")
    (tmp_path / "task-7.json").write_text("{}")

    sweep_stale_terminals(tmp_path, run=FakeRun())

    assert (tmp_path / "task-7.json").exists()
