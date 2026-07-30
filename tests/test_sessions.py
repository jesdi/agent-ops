from pathlib import Path

import dispatcher.sessions as sessions
from dispatcher.sessions import Sessions, podman_cmd, session_name


def test_session_name():
    assert sessions.session_name(42) == "task-42"


def test_is_alive(monkeypatch):
    monkeypatch.setattr(sessions, "_tmux", lambda args: 0)
    assert sessions.Sessions().is_alive(42)
    monkeypatch.setattr(sessions, "_tmux", lambda args: 1)
    assert not sessions.Sessions().is_alive(42)


def test_spawn_stage_writes_prompt_and_sends_keys(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    calls = []

    def fake_tmux(args):
        calls.append(args)
        return 1 if args[:2] == ["tmux", "has-session"] else 0

    monkeypatch.setattr(sessions, "_tmux", fake_tmux)
    sessions.Sessions().spawn_stage(42, str(tmp_path), "PROMPT BODY", "spec",
                                    "claude-fable-5")

    prompt_file = tmp_path / ".agent" / "prompt-spec.md"
    assert prompt_file.read_text() == "PROMPT BODY"
    new = next(c for c in calls if "new-session" in c)
    assert ["-s", "task-42"] == new[new.index("-s"): new.index("-s") + 2]
    assert ["-c", str(tmp_path)] == new[new.index("-c"): new.index("-c") + 2]
    send = next(c for c in calls if "send-keys" in c)
    cmd = send[-2]
    assert ('claude --remote-control task-42 --permission-mode auto '
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
    sessions.Sessions().spawn_stage(42, str(tmp_path), "P", "plan", "claude-opus-4-8")
    assert not any("new-session" in c for c in calls)
    assert any("send-keys" in c for c in calls)


def test_dry_run_touches_nothing(tmp_path: Path, monkeypatch):
    def boom(args):
        raise AssertionError("dry-run must not call tmux")

    monkeypatch.setattr(sessions, "_tmux", boom)
    s = sessions.Sessions(dry_run=True)
    s.spawn_stage(42, str(tmp_path), "P", "spec", "claude-opus-4-8")
    s.end(42)
    assert not (tmp_path / ".agent").exists()


def test_end_kills_session(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)

    subprocess_calls = []
    def fake_subprocess_run(args, **kw):
        subprocess_calls.append(args)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_subprocess_run)
    sessions.Sessions().end(42)

    assert ["tmux", "kill-session", "-t", "task-42"] in calls
    assert ["podman", "rm", "-f", "task-42"] in subprocess_calls


def test_podman_cmd_mounts_and_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    clone = tmp_path / "repos" / "pe"
    wt = tmp_path / "repos" / "pe.worktrees" / "task-42"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {clone}/.git/worktrees/task-42\n")
    cmd = podman_cmd(42, str(wt), "2g", "2", "claude-opus-4-8",
                     '"$(cat .agent/prompt-spec.md)"')
    assert cmd.startswith("podman run --rm -it --name task-42 ")
    assert "--memory 2g --cpus 2" in cmd
    assert f"-v {wt}:{wt}" in cmd
    assert f"-w {wt}" in cmd
    assert f"-v {clone}:{clone}" in cmd
    assert "-v /home/agent/agent-ops-state/claude-home:/root/.claude" in cmd
    assert cmd.endswith('claude --remote-control task-42 --permission-mode auto '
                        '--model claude-opus-4-8 "$(cat .agent/prompt-spec.md)"')


def test_resume_quotes_message(capsys):
    Sessions(dry_run=True).resume(42, "/tmp/wt", 'run said: "failure"',
                                  "claude-opus-4-8")
    out = capsys.readouterr().out
    assert "[dry-run] resume task-42" in out


def test_resume_passes_the_model(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)
    sessions.Sessions().resume(42, str(tmp_path), "carry on", "claude-sonnet-4-6")
    send = next(c for c in calls if "send-keys" in c)
    assert "--model claude-sonnet-4-6" in send[-2]
    assert "--continue" in send[-2]


def test_capture_tail_dry_run_empty():
    assert Sessions(dry_run=True).capture_tail(42) == ""


def test_idle_seconds_queries_window_activity(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "1000\n"})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    monkeypatch.setattr(sessions.time, "time", lambda: 1600.0)
    assert Sessions().idle_seconds(42) == 600.0
    assert calls == [["tmux", "display-message", "-p", "-t", "task-42",
                      "#{window_activity}"]]


def test_idle_seconds_none_on_failure_and_garbage(monkeypatch):
    fail = type("R", (), {"returncode": 1, "stdout": ""})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: fail)
    assert Sessions().idle_seconds(42) is None
    garbage = type("R", (), {"returncode": 0, "stdout": "not-a-number"})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: garbage)
    assert Sessions().idle_seconds(42) is None


def test_idle_seconds_dry_run_is_none():
    assert Sessions(dry_run=True).idle_seconds(42) is None


def test_idle_seconds_clamps_clock_skew_to_zero(monkeypatch):
    ok = type("R", (), {"returncode": 0, "stdout": "2000\n"})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: ok)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert Sessions().idle_seconds(42) == 0.0


def test_send_text_sends_literal_text_then_enter(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "_tmux", lambda args: calls.append(args) or 0)
    Sessions().send_text(42, "abc#123-code")
    assert calls == [
        ["tmux", "send-keys", "-t", "task-42", "-l", "abc#123-code"],
        ["tmux", "send-keys", "-t", "task-42", "Enter"],
    ]


def test_send_text_dry_run_touches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sessions, "_tmux",
                        lambda args: (_ for _ in ()).throw(AssertionError))
    Sessions(dry_run=True).send_text(42, "code")
    assert "[dry-run] send text to task-42" in capsys.readouterr().out


def test_capture_history_uses_S_flag_and_returns_output(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "line1\nline2\n"})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    out = Sessions().capture_history(42, lines=2000)
    assert out == "line1\nline2"
    assert calls == [["tmux", "capture-pane", "-p", "-S", "-2000",
                      "-t", "task-42"]]


def test_capture_history_empty_on_tmux_failure(monkeypatch):
    fail = type("R", (), {"returncode": 1, "stdout": ""})()
    monkeypatch.setattr(sessions.subprocess, "run", lambda *a, **k: fail)
    assert Sessions().capture_history(42) == ""


def test_capture_history_dry_run_empty():
    assert Sessions(dry_run=True).capture_history(42) == ""


def test_capture_tail_visible_pane_only_and_trims(monkeypatch):
    """Regression guard: capture_tail is the dispatcher's classification input.
    It must NOT grow an -S flag and must still trim to `lines` (default 25)."""
    body = "\n".join(str(n) for n in range(40)) + "\n"
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": body})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    out = Sessions().capture_tail(42)
    assert calls == [["tmux", "capture-pane", "-p", "-t", "task-42"]]
    assert "-S" not in calls[0]
    assert out.splitlines() == [str(n) for n in range(15, 40)]  # last 25
