"""The one-shot tmux → herdr hand-off run by provision/update.sh.

Deleted together with dispatcher/tmux_migration.py once the box has no
tmux left. No real tmux/podman is ever reached: subprocess.run is faked.
"""
from types import SimpleNamespace

import pytest

from dispatcher import tmux_migration
from dispatcher.state import Stage, TaskState, save


def _task(state_dir, issue, target="acme", park="", stage=Stage.IMPLEMENT):
    ts = TaskState(issue=issue, target=target, stage=stage, slot=0,
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

    def _resolve(self, target):
        """tmux's own `-t` resolution, prefix footgun included: `=name` is
        exact, while a BARE name matches exactly or — failing that — by
        prefix, so with only `task-acme-42` alive `-t task-acme-4` resolves
        to it. Modelled here so a call that drops the `=` kills the wrong
        session in the test too."""
        if target.startswith("="):
            name = target[1:]
            return name if name in self.sessions else None
        if target in self.sessions:
            return target
        prefixed = sorted(s for s in self.sessions if s.startswith(target))
        return prefixed[0] if prefixed else None

    def run(self, args, **kwargs):
        self.events.append(" ".join(args))
        if self.raises is not None:
            raise self.raises
        if args[0] == "tmux":
            resolved = self._resolve(args[3])
            if args[1] == "has-session":
                return SimpleNamespace(returncode=0 if resolved else 1)
            if args[1] == "kill-session":
                if resolved is None:
                    return SimpleNamespace(returncode=1)
                self.sessions.discard(resolved)
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
    start = sh.events.index("tmux kill-session -t =task-acme-1")
    assert sh.events[start:start + 4] == [
        "tmux kill-session -t =task-acme-1",
        "podman rm -f task-acme-1",
        "podman rm -f task-1",
        f"wake acme-1 {tmux_migration.MESSAGE}"]
    start = sh.events.index("tmux kill-session -t =task-2")
    assert sh.events[start:start + 4] == [
        "tmux kill-session -t =task-2",
        "podman rm -f task-acme-2",
        "podman rm -f task-2",
        f"wake acme-2 {tmux_migration.MESSAGE}"]
    # Task 3 has no session under either name: probed, never touched.
    assert "tmux kill-session -t =task-acme-3" not in sh.events
    assert "podman rm -f task-acme-3" not in sh.events

    assert "tmux kill-session -t =triage" in sh.events
    assert (state / "triage-request.json").exists()

    text = "\n".join(lines)
    for fragment in ("task-acme-1", "task-2", "triage", "container"):
        assert fragment in text, f"{fragment!r} missing from:\n{text}"
    assert "task-acme-3" not in text


def test_a_login_parked_task_with_a_live_session_migrates_too(tmp_path, shell):
    """PARK_LOGIN keeps a live session and still consumes capacity; it is
    migrated like any other in-flight task and the resumed claude
    re-prompts, so the stall/login path re-parks it."""
    state = tmp_path / "state"
    _task(state, 7, park="parked-login")
    sh = shell(sessions=["task-acme-7"], containers=["task-acme-7"])

    tmux_migration.migrate(state, sh.wake)

    assert f"wake acme-7 {tmux_migration.MESSAGE}" in sh.events


def test_a_session_name_that_is_a_prefix_of_a_live_one_is_not_confused(
        tmp_path, shell):
    """`tmux -t <name>` resolves by exact name and THEN by prefix, so a bare
    target for task 4 matches live `task-acme-42` — killing task 42's
    session and stranding it (never woken → reads dead → FAILED). Every
    target must therefore be tmux's exact form, `=<name>`."""
    state = tmp_path / "state"
    _task(state, 4)                        # no session of its own
    _task(state, 42)                       # the live one, under the new name
    _task(state, 5)                        # live under the LEGACY name only
    sh = shell(sessions=["task-acme-42", "task-51"],
               containers=["task-acme-42", "task-51"])

    tmux_migration.migrate(state, sh.wake)

    woken = sorted(e for e in sh.events if e.startswith("wake "))
    assert woken == [f"wake acme-42 {tmux_migration.MESSAGE}"]
    assert "tmux kill-session -t =task-acme-4" not in sh.events
    assert "tmux kill-session -t =task-5" not in sh.events
    assert "tmux kill-session -t =task-acme-42" in sh.events
    targets = [e.split(" -t ", 1)[1] for e in sh.events
               if e.startswith("tmux ")]
    assert all(t.startswith("=") for t in targets), targets


def test_a_live_session_the_dispatcher_would_not_drive_is_ended_not_woken(
        tmp_path, shell):
    """`_resume_woken` has no stage filter: waking a PR_OPEN task would run
    `claude --continue` in the implement transcript of a PR under review
    (and could push to it), and waking a FAILED tombstone would stand a
    container up outside capacity accounting. Both are ended only."""
    state = tmp_path / "state"
    _task(state, 8, stage=Stage.PR_OPEN)
    _task(state, 9, stage=Stage.FAILED)
    sh = shell(sessions=["task-acme-8", "task-acme-9"],
               containers=["task-acme-8", "task-acme-9"])

    lines = tmux_migration.migrate(state, sh.wake)

    assert [e for e in sh.events if e.startswith("wake ")] == []
    assert "tmux kill-session -t =task-acme-8" in sh.events
    assert "podman rm -f task-acme-8" in sh.events
    assert "podman rm -f task-8" in sh.events
    assert "tmux kill-session -t =task-acme-9" in sh.events
    text = "\n".join(lines)
    assert "ended task-acme-8 (not resumed: stage pr-open)" in text, text
    assert "ended task-acme-9 (not resumed: stage failed)" in text, text


def test_a_gate_parked_task_with_a_live_session_is_ended_not_woken(
        tmp_path, shell):
    """A CI-parked task should have no session at all; if one is somehow
    still up, end it — the wake queue is for tasks the dispatcher drives,
    and the CI wake owns this one."""
    state = tmp_path / "state"
    _task(state, 11, park="awaiting-ci")
    sh = shell(sessions=["task-acme-11"], containers=["task-acme-11"])

    lines = tmux_migration.migrate(state, sh.wake)

    assert [e for e in sh.events if e.startswith("wake ")] == []
    assert ("ended task-acme-11 (not resumed: stage implement "
            "park awaiting-ci)") in "\n".join(lines)


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
