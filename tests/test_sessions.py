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


def test_spawn_stage_writes_prompt_and_launches_in_the_task_backend(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    calls = []

    def fake_tmux(args):
        calls.append(args)
        return 0  # a live tmux session -> the tmux backend keeps driving it

    monkeypatch.setattr(sessions, "_tmux", fake_tmux)
    sessions.Sessions().spawn_stage("acme", 42, str(tmp_path), "PROMPT BODY",
                                    "spec", "claude-fable-5")
    prompt_file = tmp_path / ".agent" / "prompt-spec.md"
    assert prompt_file.read_text() == "PROMPT BODY"
    send = next(c for c in calls if "send-keys" in c)
    cmd = send[-2]
    assert ('claude --remote-control task-acme-42 --permission-mode auto '
            '--model claude-fable-5') in cmd
    assert '.agent/prompt-spec.md' in cmd
    assert send[-1] == "Enter"
    assert "HERDR_AGENT" not in cmd


def test_tmux_backend_launch_creates_the_session_when_missing(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    calls = []

    def fake_tmux(args):
        calls.append(args)
        return 1 if args[:2] == ["tmux", "has-session"] else 0

    monkeypatch.setattr(sessions, "_tmux", fake_tmux)
    sessions._TmuxBackend("2g", "2", None).launch(
        "acme", 42, str(tmp_path), "claude-fable-5", '"$(cat .agent/prompt-spec.md)"')
    new = next(c for c in calls if "new-session" in c)
    assert ["-s", "task-acme-42"] == new[new.index("-s"): new.index("-s") + 2]
    assert ["-c", str(tmp_path)] == new[new.index("-c"): new.index("-c") + 2]
    assert any("send-keys" in c for c in calls)


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


def test_end_also_removes_legacy_pre_deploy_container_name(monkeypatch):
    """Containers created before the (target, issue) rekey are named
    task-<issue> (no target). Adoption renames the tmux session, but
    nothing ever renames the container — end() must reach for both names
    or a wedged legacy container leaks until reboot."""
    monkeypatch.setattr(sessions, "_tmux", lambda args: 0)

    subprocess_calls = []
    def fake_subprocess_run(args, **kw):
        subprocess_calls.append(args)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(sessions.subprocess, "run", fake_subprocess_run)
    sessions.Sessions().end("acme", 42)

    assert ["podman", "rm", "-f", "task-acme-42"] in subprocess_calls
    assert ["podman", "rm", "-f", "task-42"] in subprocess_calls


def test_end_dry_run_does_not_remove_either_container_name(monkeypatch):
    calls = []
    def boom(args, **kw):
        calls.append(args)
        raise AssertionError("dry-run must not call podman")

    monkeypatch.setattr(sessions.subprocess, "run", boom)
    sessions.Sessions(dry_run=True).end("acme", 42)
    assert calls == []


def test_podman_cmd_mounts_and_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    clone = tmp_path / "repos" / "pe"
    wt = tmp_path / "repos" / "pe.worktrees" / "task-42"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {clone}/.git/worktrees/task-42\n")
    cmd = podman_cmd("pe", 42, str(wt), "2g", "2", "claude-opus-4-8",
                     '"$(cat .agent/prompt-spec.md)"')
    assert "podman run --rm -it --name task-pe-42 " in cmd
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


# --- herdr backend (spec §2) -------------------------------------------------
import subprocess as _sp

from dispatcher import herdr
from dispatcher.sessions import _HerdrBackend

H_TABS = ('{"id":"i","result":{"tabs":[{"label":"1","tab_id":"w1:t1",'
          '"workspace_id":"w1","number":1,"focused":true,"pane_count":1,'
          '"agent_status":"unknown"},{"label":"task-acme-42","tab_id":"w1:t2",'
          '"workspace_id":"w1","number":2,"focused":false,"pane_count":1,'
          '"agent_status":"working"}],"type":"tab_list"}}')
H_NO_TABS = '{"id":"i","result":{"tabs":[],"type":"tab_list"}}'
H_PANES = ('{"id":"i","result":{"panes":[{"pane_id":"w1:p1","tab_id":"w1:t1",'
           '"workspace_id":"w1","agent_status":"unknown","focused":true,'
           '"revision":0,"terminal_id":"a"},{"pane_id":"w1:p2","tab_id":"w1:t2",'
           '"workspace_id":"w1","agent_status":"working","focused":false,'
           '"revision":5,"terminal_id":"b"}],"type":"pane_list"}}')
H_WS = ('{"id":"i","result":{"type":"workspace_list","workspaces":[{"label":"acme",'
        '"workspace_id":"w1","number":1,"focused":true,"pane_count":2,'
        '"tab_count":2,"active_tab_id":"w1:t1","agent_status":"unknown"}]}}')
H_NO_WS = '{"id":"i","result":{"type":"workspace_list","workspaces":[]}}'
H_WS_CREATED = ('{"id":"i","result":{"workspace":{"label":"acme","workspace_id":"w1",'
                '"number":1,"focused":false,"pane_count":1,"tab_count":1,'
                '"active_tab_id":"w1:t1","agent_status":"unknown"},'
                '"tab":{"label":"1","tab_id":"w1:t1","workspace_id":"w1","number":1,'
                '"focused":false,"pane_count":1,"agent_status":"unknown"},'
                '"root_pane":{"pane_id":"w1:p1","tab_id":"w1:t1","workspace_id":"w1",'
                '"agent_status":"unknown","focused":false,"revision":0,"terminal_id":"a"},'
                '"type":"workspace_created"}}')
H_TAB_CREATED = ('{"id":"i","result":{"tab":{"label":"task-acme-42","tab_id":"w1:t2",'
                 '"workspace_id":"w1","number":2,"focused":false,"pane_count":1,'
                 '"agent_status":"unknown"},"root_pane":{"pane_id":"w1:p2",'
                 '"tab_id":"w1:t2","workspace_id":"w1","agent_status":"unknown",'
                 '"focused":false,"revision":0,"terminal_id":"b"},"type":"tab_created"}}')


def herdr_fake(monkeypatch, table, calls=None):
    """argv-prefix -> (rc, stdout) table for herdr._run; unmatched -> None."""
    def _run(args):
        if calls is not None:
            calls.append(args)
        for prefix, rc, out in table:
            if args[:len(prefix)] == list(prefix):
                return _sp.CompletedProcess(["herdr", *args], rc, out, "")
        return None
    monkeypatch.setattr(herdr, "_run", _run)


LIVE = [(("tab", "list"), 0, H_TABS), (("pane", "list"), 0, H_PANES),
        (("workspace", "list"), 0, H_WS)]


def _worktree(tmp_path):
    (tmp_path / ".git").write_text(
        f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    return str(tmp_path)


def test_herdr_is_alive_means_tab_by_label_exists(monkeypatch):
    herdr_fake(monkeypatch, LIVE)
    assert _HerdrBackend("2g", "2", None).is_alive("acme", 42) is True
    assert _HerdrBackend("2g", "2", None).is_alive("acme", 43) is False
    herdr_fake(monkeypatch, [])  # server down reads dead, as a dead tmux server did
    assert _HerdrBackend("2g", "2", None).is_alive("acme", 42) is False


def test_herdr_launch_creates_workspace_and_tab_on_first_use(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake(monkeypatch, [
        (("tab", "list"), 0, H_NO_TABS), (("workspace", "list"), 0, H_NO_WS),
        (("workspace", "create"), 0, H_WS_CREATED),
        (("tab", "create"), 0, H_TAB_CREATED), (("pane", "run"), 0, "")], calls)
    _HerdrBackend("2g", "2", None).launch("acme", 42, wt, "claude-fable-5",
                                          '"$(cat .agent/prompt-spec.md)"')
    assert ["workspace", "create", "--label", "acme",
            "--cwd", f"{tmp_path}/clone", "--no-focus"] in calls
    assert ["tab", "create", "--workspace", "w1", "--label", "task-acme-42",
            "--cwd", wt, "--no-focus"] in calls
    run = next(c for c in calls if c[:2] == ["pane", "run"])
    assert run[2] == "w1:p2"
    assert run[3].startswith("HERDR_AGENT=claude ")
    assert "with-claude-token.sh podman run --rm -it --name task-acme-42 " in run[3]
    assert ('claude --remote-control task-acme-42 --permission-mode auto '
            '--model claude-fable-5 "$(cat .agent/prompt-spec.md)"') in run[3]


def test_herdr_launch_reuses_existing_workspace_and_tab(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "run"), 0, "")], calls)
    _HerdrBackend("2g", "2", None).launch("acme", 42, wt, "m", "--continue x")
    assert not any(c[:2] in (["workspace", "create"], ["tab", "create"])
                   for c in calls)
    assert calls[-1][:3] == ["pane", "run", "w1:p2"]


def test_herdr_launch_is_a_noop_when_the_server_is_down(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake(monkeypatch, [], calls)
    _HerdrBackend("2g", "2", None).launch("acme", 42, wt, "m", "x")  # no raise
    assert not any(c[:2] == ["pane", "run"] for c in calls)


def test_herdr_agent_prefix_is_not_in_containers_session_cmd(tmp_path):
    wt = _worktree(tmp_path)
    assert "HERDR_AGENT" not in podman_cmd("acme", 42, wt, "2g", "2", "m", "x")


def test_herdr_capture_tail_reads_visible_and_trims(monkeypatch):
    body = "\n".join(str(n) for n in range(40)) + "\n"
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, body)], calls)
    out = _HerdrBackend("2g", "2", None).capture_tail("acme", 42)
    assert calls[-1] == ["pane", "read", "w1:p2", "--source", "visible",
                         "--lines", "25"]
    assert out.splitlines() == [str(n) for n in range(15, 40)]


def test_herdr_capture_history_reads_recent_unwrapped(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, "line1\nline2\n")], calls)
    out = _HerdrBackend("2g", "2", None).capture_history("acme", 42, lines=500)
    assert out == "line1\nline2"
    assert calls[-1] == ["pane", "read", "w1:p2", "--source", "recent-unwrapped",
                         "--lines", "500"]


def test_herdr_capture_empty_on_failure_or_dead(monkeypatch):
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 1, '{"error":{"code":"pane_not_found"},"id":"i"}')])
    b = _HerdrBackend("2g", "2", None)
    assert b.capture_tail("acme", 42) == ""
    assert b.capture_history("acme", 42) == ""
    herdr_fake(monkeypatch, [(("tab", "list"), 0, H_NO_TABS)])
    assert b.capture_tail("acme", 42) == ""
    herdr_fake(monkeypatch, [])
    assert b.capture_history("acme", 42) == ""


def test_herdr_send_text_types_literal_then_enter(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "send-text"), 0, ""),
                                    (("pane", "send-keys"), 0, "")], calls)
    _HerdrBackend("2g", "2", None).send_text("acme", 42, "abc#123-code")
    assert calls[-2:] == [["pane", "send-text", "w1:p2", "abc#123-code"],
                          ["pane", "send-keys", "w1:p2", "enter"]]


def test_herdr_send_text_noop_when_dead(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, [(("tab", "list"), 0, H_NO_TABS)], calls)
    _HerdrBackend("2g", "2", None).send_text("acme", 42, "code")
    assert not any(c[1].startswith("send") for c in calls)


def test_herdr_end_closes_tab_then_removes_both_container_names(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("tab", "close"), 0, '{"id":"i","result":{"type":"ok"}}')], calls)
    podman = []
    monkeypatch.setattr(sessions.subprocess, "run",
                        lambda args, **kw: podman.append(args) or _sp.CompletedProcess(args, 0, "", ""))
    _HerdrBackend("2g", "2", None).end("acme", 42)
    assert ["tab", "close", "w1:t2"] in calls
    assert podman == [["podman", "rm", "-f", "task-acme-42"],
                      ["podman", "rm", "-f", "task-42"]]


def test_herdr_end_on_a_dead_task_still_removes_containers(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, [(("tab", "list"), 0, H_NO_TABS)], calls)
    podman = []
    monkeypatch.setattr(sessions.subprocess, "run",
                        lambda args, **kw: podman.append(args) or _sp.CompletedProcess(args, 0, "", ""))
    _HerdrBackend("2g", "2", None).end("acme", 42)
    assert not any(c[:2] == ["tab", "close"] for c in calls)
    assert ["podman", "rm", "-f", "task-acme-42"] in podman
    assert ["podman", "rm", "-f", "task-42"] in podman


# --- herdr idle_seconds (spec §3) --------------------------------------------
import json as _json


def _agent(status, seq):
    return (("agent", "get"), 0,
            '{"id":"i","result":{"agent":{"agent_status":"%s","state_change_seq":%d,'
            '"pane_id":"w1:p2"},"type":"agent_info"}}' % (status, seq))


def test_idle_working_is_zero_and_writes_no_sidecar(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, LIVE + [_agent("working", 3)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert _HerdrBackend("2g", "2", tmp_path).idle_seconds("acme", 42) == 0.0
    assert not (tmp_path / "herdr-status").exists()


def test_idle_new_status_pair_starts_the_clock(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 4)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert _HerdrBackend("2g", "2", tmp_path).idle_seconds("acme", 42) == 0.0
    side = _json.loads((tmp_path / "herdr-status" / "task-acme-42.json").read_text())
    assert side == {"seq": 4, "status": "idle", "since": 1000.0}


def test_idle_same_pair_accumulates_elapsed(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, LIVE + [_agent("blocked", 4)])
    b = _HerdrBackend("2g", "2", tmp_path)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert b.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1650.0)
    assert b.idle_seconds("acme", 42) == 650.0


def test_idle_resets_when_seq_or_status_changes(tmp_path, monkeypatch):
    b = _HerdrBackend("2g", "2", tmp_path)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 4)])
    b.idle_seconds("acme", 42)
    monkeypatch.setattr(sessions.time, "time", lambda: 1500.0)
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 5)])   # new seq, same status
    assert b.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1600.0)
    herdr_fake(monkeypatch, LIVE + [_agent("blocked", 5)])  # same seq, new status
    assert b.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1700.0)
    assert b.idle_seconds("acme", 42) == 100.0


def test_idle_missing_agent_accumulates_like_a_static_screen(tmp_path, monkeypatch):
    # claude exited: the pane is back at the host shell, herdr sees no
    # agent. That is "static" — idle time must accumulate so the stall
    # timer eventually parks it, exactly as tmux's window_activity did.
    herdr_fake(monkeypatch, LIVE + [(("agent", "get"), 1,
               '{"error":{"code":"agent_not_found","message":"x"},"id":"i"}')])
    b = _HerdrBackend("2g", "2", tmp_path)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert b.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1900.0)
    assert b.idle_seconds("acme", 42) == 900.0


def test_idle_none_when_dead_server_down_or_no_state_dir(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, [(("tab", "list"), 0, H_NO_TABS)])
    assert _HerdrBackend("2g", "2", tmp_path).idle_seconds("acme", 42) is None
    herdr_fake(monkeypatch, LIVE + [(("agent", "get"), 1,
               '{"error":{"code":"server_not_running"},"id":"i"}')])
    assert _HerdrBackend("2g", "2", tmp_path).idle_seconds("acme", 42) is None
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    assert _HerdrBackend("2g", "2", None).idle_seconds("acme", 42) is None


def test_idle_none_on_sidecar_write_failure_never_a_stale_number(tmp_path, monkeypatch):
    (tmp_path / "herdr-status").write_text("not a dir")  # mkdir/write -> OSError
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    assert _HerdrBackend("2g", "2", tmp_path).idle_seconds("acme", 42) is None


def test_idle_corrupt_sidecar_is_rewritten_not_fatal(tmp_path, monkeypatch):
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text("{garbage")
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert _HerdrBackend("2g", "2", tmp_path).idle_seconds("acme", 42) == 0.0
    assert _json.loads(side.read_text())["since"] == 1000.0


def test_idle_clamps_clock_skew_to_zero(tmp_path, monkeypatch):
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text(_json.dumps({"seq": 1, "status": "idle", "since": 2000.0}))
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert _HerdrBackend("2g", "2", tmp_path).idle_seconds("acme", 42) == 0.0


def test_forget_status_removes_the_sidecar(tmp_path):
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text("{}")
    _HerdrBackend("2g", "2", tmp_path).forget_status("acme", 42)
    assert not side.exists()
    _HerdrBackend("2g", "2", tmp_path).forget_status("acme", 42)  # idempotent
    _HerdrBackend("2g", "2", None).forget_status("acme", 42)      # no state dir


# --- backend selection: adopt-on-touch (spec §7) -----------------------------

def _tmux_table(monkeypatch, alive: set[str], calls=None):
    """`tmux has-session -t <name>` succeeds for names in `alive`."""
    def fake_tmux(args):
        if calls is not None:
            calls.append(args)
        if args[:2] == ["tmux", "has-session"]:
            return 0 if args[-1] in alive else 1
        return 0
    monkeypatch.setattr(sessions, "_tmux", fake_tmux)


def test_live_tmux_session_selects_the_tmux_backend(monkeypatch):
    _tmux_table(monkeypatch, {"task-acme-42"})
    s = sessions.Sessions()
    assert isinstance(s._backend("acme", 42), sessions._TmuxBackend)


def test_legacy_tmux_name_selects_the_tmux_backend(monkeypatch):
    _tmux_table(monkeypatch, {"task-42"})
    s = sessions.Sessions()
    assert isinstance(s._backend("acme", 42), sessions._TmuxBackend)


def test_no_tmux_session_selects_herdr(monkeypatch):
    _tmux_table(monkeypatch, set())
    s = sessions.Sessions()
    assert isinstance(s._backend("acme", 42), sessions._HerdrBackend)


def test_tmux_binary_absent_fails_closed_to_herdr(monkeypatch):
    def no_tmux(*a, **k):
        raise FileNotFoundError("tmux")
    monkeypatch.setattr(sessions.subprocess, "run", no_tmux)
    s = sessions.Sessions()
    assert isinstance(s._backend("acme", 42), sessions._HerdrBackend)


def test_parked_task_resumes_in_herdr_even_if_another_task_is_on_tmux(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    _tmux_table(monkeypatch, {"task-acme-41"})
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "run"), 0, "")], calls)
    sessions.Sessions().resume("acme", 42, str(tmp_path), "carry on", "claude-sonnet-5")
    run = next(c for c in calls if c[:2] == ["pane", "run"])
    assert "--continue" in run[3] and "--model claude-sonnet-5" in run[3]


def test_facade_routes_every_method_to_the_selected_backend(tmp_path, monkeypatch):
    class Spy:
        def __init__(self):
            self.calls = []
        def __getattr__(self, name):
            def rec(*a, **k):
                self.calls.append((name, a))
                return {"is_alive": True, "capture_tail": "t",
                        "capture_history": "h", "idle_seconds": 1.0}.get(name)
            return rec

    spy = Spy()
    s = sessions.Sessions(state_dir=tmp_path)  # a state_dir so end() snapshots
    monkeypatch.setattr(s, "_backend", lambda target, issue: spy)
    assert s.is_alive("acme", 42) is True
    assert s.capture_tail("acme", 42) == "t"
    assert s.capture_history("acme", 42, lines=10) == "h"
    assert s.idle_seconds("acme", 42) == 1.0
    s.send_text("acme", 42, "x")
    s.end("acme", 42)
    names = [n for n, _ in spy.calls]
    assert names == ["is_alive", "capture_tail", "capture_history",
                     "idle_seconds", "send_text", "capture_history", "end"]
    # end() snapshots (capture_history) BEFORE the backend end — same order as today


def test_end_on_herdr_snapshots_then_closes_and_forgets_status(tmp_path, monkeypatch):
    _tmux_table(monkeypatch, set())
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text("{}")
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, "last words\n"),
                                    (("tab", "close"), 0, '{"id":"i","result":{"type":"ok"}}')], calls)
    monkeypatch.setattr(sessions.subprocess, "run",
                        lambda args, **kw: _sp.CompletedProcess(args, 0, "", ""))
    sessions.Sessions(state_dir=tmp_path).end("acme", 42)
    assert (tmp_path / "snapshots" / "task-acme-42.txt").read_text() == "last words"
    read_i = next(i for i, c in enumerate(calls) if c[:2] == ["pane", "read"])
    close_i = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "close"])
    assert read_i < close_i
    assert not side.exists()
