"""The one-shot tmux → herdr hand-off run by provision/update.sh.

Deleted together with dispatcher/tmux_migration.py once the box has no
tmux left. No real tmux/podman is ever reached: subprocess.run is faked.
"""
from types import SimpleNamespace

import pytest

from dispatcher import tmux_migration
from dispatcher.state import Stage, TaskState, save


def _task(state_dir, issue, target="acme", park=""):
    ts = TaskState(issue=issue, target=target, stage=Stage.IMPLEMENT, slot=0,
                   worktree=f"/wt/task-{issue}", branch=f"agent/task-{issue}",
                   title=f"task {issue}", updated_at="2026-09-03T00:00:00Z",
                   park=park)
    save(state_dir, ts)
    return ts


class FakeShell:
    """tmux/podman stand-in recording every call into a shared event log,
    so ordering across tmux, podman and wake() is assertable."""

    def __init__(self, sessions=(), containers=(), raises=None):
        self.sessions = set(sessions)
        self.containers = set(containers)
        self.raises = raises
        self.events = []

    def run(self, args, **kwargs):
        self.events.append(" ".join(args))
        if self.raises is not None:
            raise self.raises
        if args[0] == "tmux":
            name = args[3]
            if args[1] == "has-session":
                return SimpleNamespace(returncode=0 if name in self.sessions else 1)
            if args[1] == "kill-session":
                self.sessions.discard(name)
                return SimpleNamespace(returncode=0)
        if args[:3] == ["podman", "rm", "-f"]:
            return SimpleNamespace(returncode=0 if args[3] in self.containers else 1)
        raise AssertionError(f"unexpected call: {args}")

    def wake(self, task, text):
        self.events.append(f"wake {task.target}-{task.issue} {text}")


@pytest.fixture
def shell(monkeypatch):
    def install(**kw):
        sh = FakeShell(**kw)
        monkeypatch.setattr(tmux_migration.subprocess, "run", sh.run)
        return sh
    return install


def test_hands_every_live_tmux_task_to_the_park_resume_path(tmp_path, shell):
    state = tmp_path / "state"
    _task(state, 1)                      # live under the new name
    _task(state, 2)                      # live under the legacy name
    _task(state, 3)                      # no session at all
    sh = shell(sessions=["task-acme-1", "task-2", "triage"],
               containers=["task-acme-1", "task-2"])

    lines = tmux_migration.migrate(state, sh.wake)

    woken = sorted(e for e in sh.events if e.startswith("wake "))
    assert woken == [f"wake acme-1 {tmux_migration.MESSAGE}",
                     f"wake acme-2 {tmux_migration.MESSAGE}"]
    # Per task: kill the session(s) that exist, then remove BOTH container
    # names, then wake — the wake must be last, so a crash mid-teardown
    # cannot leave a task queued for resume with its old session still up.
    start = sh.events.index("tmux kill-session -t task-acme-1")
    assert sh.events[start:start + 4] == [
        "tmux kill-session -t task-acme-1",
        "podman rm -f task-acme-1",
        "podman rm -f task-1",
        f"wake acme-1 {tmux_migration.MESSAGE}"]
    start = sh.events.index("tmux kill-session -t task-2")
    assert sh.events[start:start + 4] == [
        "tmux kill-session -t task-2",
        "podman rm -f task-acme-2",
        "podman rm -f task-2",
        f"wake acme-2 {tmux_migration.MESSAGE}"]
    # Task 3 has no session under either name: probed, never touched.
    assert "tmux kill-session -t task-acme-3" not in sh.events
    assert "podman rm -f task-acme-3" not in sh.events

    assert "tmux kill-session -t triage" in sh.events
    assert (state / "triage-request.json").exists()

    text = "\n".join(lines)
    for fragment in ("task-acme-1", "task-2", "triage", "container"):
        assert fragment in text, f"{fragment!r} missing from:\n{text}"
    assert "task-acme-3" not in text


def test_a_parked_task_with_a_live_session_migrates_too(tmp_path, shell):
    """PARK_LOGIN keeps a live session; it is migrated like any other and
    the resumed claude re-prompts, so the stall/login path re-parks it."""
    state = tmp_path / "state"
    _task(state, 7, park="parked-login")
    sh = shell(sessions=["task-acme-7"], containers=["task-acme-7"])

    tmux_migration.migrate(state, sh.wake)

    assert f"wake acme-7 {tmux_migration.MESSAGE}" in sh.events


def test_missing_tmux_binary_migrates_nothing(tmp_path, shell):
    state = tmp_path / "state"
    _task(state, 1)
    sh = shell(sessions=["task-acme-1", "triage"],
               raises=FileNotFoundError("tmux"))

    lines = tmux_migration.migrate(state, sh.wake)

    assert lines == []
    assert all(e.startswith("tmux has-session") for e in sh.events), sh.events
    assert not (state / "triage-request.json").exists()


def test_a_failed_podman_removal_is_logged_not_raised(tmp_path, shell):
    """The container may already be gone (the common case) or podman may be
    wedged: neither may abort the deploy pass mid-migration."""
    state = tmp_path / "state"
    _task(state, 1)
    sh = shell(sessions=["task-acme-1"], containers=[])

    lines = tmux_migration.migrate(state, sh.wake)

    assert f"wake acme-1 {tmux_migration.MESSAGE}" in sh.events
    assert any("task-acme-1" in l and "container" in l for l in lines)


def test_no_triage_session_leaves_the_sweep_queue_alone(tmp_path, shell):
    state = tmp_path / "state"
    sh = shell(sessions=[])

    assert tmux_migration.migrate(state, sh.wake) == []
    assert not (state / "triage-request.json").exists()
