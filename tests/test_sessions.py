from pathlib import Path

import dispatcher.sessions as sessions
from dispatcher.sessions import Sessions, podman_cmd, session_name


def test_session_name_includes_target():
    assert sessions.session_name("agent_ops", 7) == "task-agent_ops-7"


def test_legacy_session_adopted_by_rename(monkeypatch):
    calls = []

    def fake_tmux(args):
        calls.append(args)
        if args[:2] == ["tmux", "has-session"]:
            # legacy alive, new name not
            return 0 if args[-1] == "task-7" else 1
        return 0

    monkeypatch.setattr(sessions, "_tmux", fake_tmux)
    s = sessions.Sessions(dry_run=False)
    assert s.is_alive("portfolio_eval", 7)
    assert ["tmux", "rename-session", "-t", "task-7",
            "task-portfolio_eval-7"] in calls


def test_is_alive(monkeypatch):
    monkeypatch.setattr(sessions, "_tmux", lambda args: 0)
    assert sessions.Sessions().is_alive("acme", 42)
    monkeypatch.setattr(sessions, "_tmux", lambda args: 1)
    assert not sessions.Sessions().is_alive("acme", 42)


def test_spawn_stage_writes_prompt_and_sends_keys(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    calls = []

    def fake_tmux(args):
        calls.append(args)
        return 1 if args[:2] == ["tmux", "has-session"] else 0

    monkeypatch.setattr(sessions, "_tmux", fake_tmux)
    sessions.Sessions().spawn_stage("acme", 42, str(tmp_path), "PROMPT BODY",
                                    "spec", "claude-fable-5")

    prompt_file = tmp_path / ".agent" / "prompt-spec.md"
    assert prompt_file.read_text() == "PROMPT BODY"
    new = next(c for c in calls if "new-session" in c)
    assert ["-s", "task-acme-42"] == new[new.index("-s"): new.index("-s") + 2]
    assert ["-c", str(tmp_path)] == new[new.index("-c"): new.index("-c") + 2]
    send = next(c for c in calls if "send-keys" in c)
    cmd = send[-2]
    assert ('claude --remote-control task-acme-42 --permission-mode auto '
            '--model claude-fable-5') in cmd
    assert '.agent/prompt-spec.md' in cmd
    assert send[-1] == "Enter"


def test_spawn_stage_reuses_existing_session(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    calls = []

    def fake_tmux(args):
        calls.append(args)
        return 0  # has-session says alive

    monkeypatch.setattr(sessions, "_tmux", fake_tmux)
    sessions.Sessions().spawn_stage("acme", 42, str(tmp_path), "P", "plan",
                                    "claude-opus-4-8")
    assert not any("new-session" in c for c in calls)
    assert any("send-keys" in c for c in calls)


def test_dry_run_touches_nothing(tmp_path: Path, monkeypatch):
    def boom(args):
        raise AssertionError("dry-run must not call tmux")

    monkeypatch.setattr(sessions, "_tmux", boom)
    s = sessions.Sessions(dry_run=True)
    s.spawn_stage("acme", 42, str(tmp_path), "P", "spec", "claude-opus-4-8")
    s.end("acme", 42)
    assert not (tmp_path / ".agent").exists()


def test_end_kills_session(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)

    subprocess_calls = []
    def fake_subprocess_run(args, **kw):
        subprocess_calls.append(args)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_subprocess_run)
    sessions.Sessions().end("acme", 42)

    assert ["tmux", "kill-session", "-t", "task-acme-42"] in calls
    assert ["podman", "rm", "-f", "task-acme-42"] in subprocess_calls


def test_podman_cmd_mounts_and_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    clone = tmp_path / "repos" / "pe"
    wt = tmp_path / "repos" / "pe.worktrees" / "task-42"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {clone}/.git/worktrees/task-42\n")
    cmd = podman_cmd("pe", 42, str(wt), "2g", "2", "claude-opus-4-8",
                     '"$(cat .agent/prompt-spec.md)"')
    assert cmd.startswith("podman run --rm -it --name task-pe-42 ")
    assert "--memory 2g --cpus 2" in cmd
    assert f"-v {wt}:{wt}" in cmd
    assert f"-w {wt}" in cmd
    assert f"-v {clone}:{clone}" in cmd
    assert "-v /home/agent/agent-ops-state/claude-home:/root/.claude" in cmd
    assert cmd.endswith('claude --remote-control task-pe-42 --permission-mode auto '
                        '--model claude-opus-4-8 "$(cat .agent/prompt-spec.md)"')


def test_resume_quotes_message(capsys):
    Sessions(dry_run=True).resume("acme", 42, "/tmp/wt", 'run said: "failure"',
                                  "claude-opus-4-8")
    out = capsys.readouterr().out
    assert "[dry-run] resume task-acme-42" in out


def test_resume_passes_the_model(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)
    sessions.Sessions().resume("acme", 42, str(tmp_path), "carry on",
                               "claude-sonnet-4-6")
    send = next(c for c in calls if "send-keys" in c)
    assert "--model claude-sonnet-4-6" in send[-2]
    assert "--continue" in send[-2]


def test_capture_tail_dry_run_empty():
    assert Sessions(dry_run=True).capture_tail("acme", 42) == ""


def test_idle_seconds_queries_window_activity(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "1000\n"})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    monkeypatch.setattr(sessions.time, "time", lambda: 1600.0)
    assert Sessions().idle_seconds("acme", 42) == 600.0
    assert calls[-1] == ["tmux", "display-message", "-p", "-t", "task-acme-42",
                         "#{window_activity}"]


def test_idle_seconds_none_on_failure_and_garbage(monkeypatch):
    fail = type("R", (), {"returncode": 1, "stdout": ""})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: fail)
    assert Sessions().idle_seconds("acme", 42) is None
    garbage = type("R", (), {"returncode": 0, "stdout": "not-a-number"})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: garbage)
    assert Sessions().idle_seconds("acme", 42) is None


def test_idle_seconds_dry_run_is_none():
    assert Sessions(dry_run=True).idle_seconds("acme", 42) is None


def test_idle_seconds_clamps_clock_skew_to_zero(monkeypatch):
    ok = type("R", (), {"returncode": 0, "stdout": "2000\n"})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: ok)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert Sessions().idle_seconds("acme", 42) == 0.0


def test_send_text_sends_literal_text_then_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)
    Sessions().send_text("acme", 42, "abc#123-code")
    assert calls[-2:] == [
        ["tmux", "send-keys", "-t", "task-acme-42", "-l", "abc#123-code"],
        ["tmux", "send-keys", "-t", "task-acme-42", "Enter"],
    ]


def test_send_text_dry_run_touches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "_tmux",
                        lambda args: (_ for _ in ()).throw(AssertionError))
    Sessions(dry_run=True).send_text("acme", 42, "code")
    assert "[dry-run] send text to task-acme-42" in capsys.readouterr().out


def test_capture_history_uses_S_flag_and_returns_output(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "line1\nline2\n"})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    out = Sessions().capture_history("acme", 42, lines=2000)
    assert out == "line1\nline2"
    assert calls[-1] == ["tmux", "capture-pane", "-p", "-S", "-2000",
                         "-t", "task-acme-42"]


def test_capture_history_empty_on_tmux_failure(monkeypatch):
    fail = type("R", (), {"returncode": 1, "stdout": ""})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: fail)
    assert Sessions().capture_history("acme", 42) == ""


def test_capture_history_dry_run_empty():
    assert Sessions(dry_run=True).capture_history("acme", 42) == ""


def test_capture_tail_visible_pane_only_and_trims(monkeypatch):
    """Regression guard: capture_tail is the dispatcher's classification input.
    It must NOT grow an -S flag and must still trim to `lines` (default 25)."""
    body = "\n".join(str(n) for n in range(40)) + "\n"
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": body})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    out = Sessions().capture_tail("acme", 42)
    assert calls[-1] == ["tmux", "capture-pane", "-p", "-t", "task-acme-42"]
    assert "-S" not in calls[-1]
    assert out.splitlines() == [str(n) for n in range(15, 40)]  # last 25


def _fake_run_factory(calls, history="line1\nline2\n"):
    def fake_run(args, **kw):
        calls.append(args)
        if args[:2] == ["tmux", "capture-pane"]:
            return type("R", (), {"returncode": 0, "stdout": history})()
        return type("R", (), {"returncode": 0, "stdout": ""})()
    return fake_run


def test_end_snapshots_history_before_kill(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(sessions.subprocess, "run", _fake_run_factory(calls))
    sessions.Sessions(state_dir=tmp_path).end("acme", 42)
    snap = tmp_path / "snapshots" / "task-acme-42.txt"
    assert snap.read_text() == "line1\nline2"  # capture_history rstrips
    capture_i = next(i for i, c in enumerate(calls)
                     if c[:2] == ["tmux", "capture-pane"])
    kill_i = calls.index(["tmux", "kill-session", "-t", "task-acme-42"])
    assert capture_i < kill_i


def test_end_empty_capture_keeps_existing_snapshot(tmp_path, monkeypatch):
    # end() runs again on already-dead sessions (e.g. _resume_woken ends
    # before resuming) — an empty capture must not truncate the park's snapshot.
    snap = tmp_path / "snapshots" / "task-acme-42.txt"
    snap.parent.mkdir(parents=True)
    snap.write_text("the parked question")
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: 0)
    monkeypatch.setattr(sessions.subprocess, "run",
                        _fake_run_factory(calls, history=""))
    sessions.Sessions(state_dir=tmp_path).end("acme", 42)
    assert snap.read_text() == "the parked question"


def test_end_without_state_dir_writes_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: 0)
    monkeypatch.setattr(sessions.subprocess, "run", _fake_run_factory(calls))
    sessions.Sessions().end("acme", 42)
    assert not (tmp_path / "snapshots").exists()


def test_end_snapshot_write_failure_still_kills(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(sessions.subprocess, "run", _fake_run_factory(calls))
    # state_dir/snapshots exists as a FILE -> mkdir/write raises OSError
    (tmp_path / "snapshots").write_text("not a dir")
    sessions.Sessions(state_dir=tmp_path).end("acme", 42)
    assert ["tmux", "kill-session", "-t", "task-acme-42"] in calls


def test_end_wedged_tmux_still_kills(tmp_path, monkeypatch):
    """Regression guard: capture_history can raise subprocess.TimeoutExpired
    on a wedged tmux server. This must not propagate out of end() — the
    session must still be killed."""
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)

    def fake_run_with_timeout(args, **kw):
        calls.append(args)
        if args[:2] == ["tmux", "capture-pane"]:
            raise sessions.subprocess.TimeoutExpired(cmd="tmux", timeout=30)
        return type("R", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run_with_timeout)
    sessions.Sessions(state_dir=tmp_path).end("acme", 42)
    assert ["tmux", "kill-session", "-t", "task-acme-42"] in calls
    assert not (tmp_path / "snapshots").exists()
