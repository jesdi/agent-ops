"""dispatcher/sessions.py: the task session as a herdr tab. Every test
drives the real `Sessions` over a faked `herdr._run` (an argv-prefix ->
(returncode, stdout) table), so both the degrade contract and the exact
CLI calls are asserted."""
import json as _json
import subprocess as _sp

import dispatcher.sessions as sessions
from dispatcher import herdr
from dispatcher.sessions import Sessions, podman_cmd

# --- herdr CLI replies -------------------------------------------------------
# w1:t2/w1:p2 is the tab that already exists under the label; w1:t3/w1:p3 is
# the one `tab create` makes.

TABS = ('{"id":"i","result":{"tabs":[{"label":"1","tab_id":"w1:t1",'
        '"workspace_id":"w1","number":1,"focused":true,"pane_count":1,'
        '"agent_status":"unknown"},{"label":"task-acme-42","tab_id":"w1:t2",'
        '"workspace_id":"w1","number":2,"focused":false,"pane_count":1,'
        '"agent_status":"working"}],"type":"tab_list"}}')
TABS_FRESH = ('{"id":"i","result":{"tabs":[{"label":"1","tab_id":"w1:t1",'
              '"workspace_id":"w1","number":1,"focused":true,"pane_count":1,'
              '"agent_status":"unknown"},{"label":"task-acme-42","tab_id":"w1:t3",'
              '"workspace_id":"w1","number":3,"focused":false,"pane_count":1,'
              '"agent_status":"unknown"}],"type":"tab_list"}}')
NO_TABS = '{"id":"i","result":{"tabs":[],"type":"tab_list"}}'
PANES = ('{"id":"i","result":{"panes":[{"pane_id":"w1:p1","tab_id":"w1:t1",'
         '"workspace_id":"w1","agent_status":"unknown","focused":true,'
         '"revision":0,"terminal_id":"a"},{"pane_id":"w1:p2","tab_id":"w1:t2",'
         '"workspace_id":"w1","agent_status":"working","focused":false,'
         '"revision":5,"terminal_id":"b"},{"pane_id":"w1:p3","tab_id":"w1:t3",'
         '"workspace_id":"w1","agent_status":"unknown","focused":false,'
         '"revision":0,"terminal_id":"c"}],"type":"pane_list"}}')
WS = ('{"id":"i","result":{"type":"workspace_list","workspaces":[{"label":"acme",'
      '"workspace_id":"w1","number":1,"focused":true,"pane_count":2,'
      '"tab_count":2,"active_tab_id":"w1:t1","agent_status":"unknown"}]}}')
NO_WS = '{"id":"i","result":{"type":"workspace_list","workspaces":[]}}'
WS_CREATED = ('{"id":"i","result":{"workspace":{"label":"acme","workspace_id":"w1",'
              '"number":1,"focused":false,"pane_count":1,"tab_count":1,'
              '"active_tab_id":"w1:t1","agent_status":"unknown"},'
              '"tab":{"label":"1","tab_id":"w1:t1","workspace_id":"w1","number":1,'
              '"focused":false,"pane_count":1,"agent_status":"unknown"},'
              '"root_pane":{"pane_id":"w1:p1","tab_id":"w1:t1","workspace_id":"w1",'
              '"agent_status":"unknown","focused":false,"revision":0,"terminal_id":"a"},'
              '"type":"workspace_created"}}')
TAB_CREATED = ('{"id":"i","result":{"tab":{"label":"task-acme-42","tab_id":"w1:t3",'
               '"workspace_id":"w1","number":3,"focused":false,"pane_count":1,'
               '"agent_status":"unknown"},"root_pane":{"pane_id":"w1:p3",'
               '"tab_id":"w1:t3","workspace_id":"w1","agent_status":"unknown",'
               '"focused":false,"revision":0,"terminal_id":"c"},"type":"tab_created"}}')
CLOSE_OK = '{"id":"i","result":{"type":"ok"}}'


def _process_info(shell_pid, pgid):
    return ('{"id":"i","result":{"process_info":{"foreground_process_group_id":%d,'
            '"foreground_processes":[{"name":"x","pid":%d}],"pane_id":"w1:p2",'
            '"shell_pid":%d},"type":"pane_process_info"}}' % (pgid, pgid, shell_pid))


BUSY = (("pane", "process-info"), 0, _process_info(100, 200))
AT_PROMPT = (("pane", "process-info"), 0, _process_info(100, 100))


def herdr_fake(monkeypatch, table, calls=None):
    """argv-prefix -> (rc, stdout) table for herdr._run; unmatched -> None
    (the "no server" degrade the autouse conftest fixture also installs)."""
    def _run(args):
        if calls is not None:
            calls.append(args)
        for prefix, rc, out in table:
            if args[:len(prefix)] == list(prefix):
                return _sp.CompletedProcess(["herdr", *args], rc, out, "")
        return None
    monkeypatch.setattr(herdr, "_run", _run)


def herdr_fake_creating(monkeypatch, calls, tabs=NO_TABS, workspaces=NO_WS):
    """A server that remembers its own writes, which a flat table cannot:
    `Tab.ensure` re-resolves by label after creating, so `tab list` must
    start at `tabs` and report the fresh tab afterwards. Any tab under the
    label is at its prompt (idle), so ensure closes it first."""
    made = {"tab": False, "workspace": False}

    def _run(args):
        calls.append(args)

        def ok(out):
            return _sp.CompletedProcess(["herdr", *args], 0, out, "")

        head = args[:2]
        if head == ["tab", "list"]:
            return ok(TABS_FRESH if made["tab"] else tabs)
        if head == ["pane", "list"]:
            return ok(PANES)
        if head == ["pane", "process-info"]:
            return ok(_process_info(100, 100))
        if head == ["tab", "close"]:
            return ok(CLOSE_OK)
        if head == ["workspace", "list"]:
            return ok(WS if made["workspace"] else workspaces)
        if head == ["workspace", "create"]:
            made["workspace"] = True
            return ok(WS_CREATED)
        if head == ["tab", "create"]:
            made["tab"] = True
            return ok(TAB_CREATED)
        if head == ["pane", "run"]:
            return ok("")
        return None

    monkeypatch.setattr(herdr, "_run", _run)


#: a task-acme-42 tab that exists and whose shell is busy.
LIVE = [(("tab", "list"), 0, TABS), (("pane", "list"), 0, PANES),
        (("workspace", "list"), 0, WS), BUSY]


def _worktree(tmp_path):
    """A worktree whose .git points into a clone — containers.session_cmd
    reads it to derive the clone mount."""
    (tmp_path / ".git").write_text(
        f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")
    return str(tmp_path)


def _fake_podman(monkeypatch, seen=None):
    """end() shells out to podman; record the argv instead of running it."""
    def run(args, **kw):
        if seen is not None:
            seen.append(args)
        return _sp.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(sessions.subprocess, "run", run)


# --- naming and the container command ----------------------------------------

def test_session_name_includes_target():
    assert sessions.session_name("agent_ops", 7) == "task-agent_ops-7"


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


def test_herdr_agent_is_never_in_the_container_command(tmp_path):
    """HERDR_AGENT is tab-level env, never a command prefix — containers.py
    is shared with the headless triage run, which must NOT read as an agent."""
    assert "HERDR_AGENT" not in podman_cmd("acme", 42, _worktree(tmp_path),
                                           "2g", "2", "m", "x")


# --- is_alive ----------------------------------------------------------------

def test_is_alive_true_only_for_a_tab_whose_shell_is_busy(monkeypatch):
    herdr_fake(monkeypatch, LIVE)
    assert Sessions().is_alive("acme", 42) is True
    assert Sessions().is_alive("acme", 43) is False  # no tab under that label
    herdr_fake(monkeypatch, [(("tab", "list"), 0, NO_TABS)])
    assert Sessions().is_alive("acme", 42) is False
    herdr_fake(monkeypatch, [])  # server down reads dead
    assert Sessions().is_alive("acme", 42) is False


def test_is_alive_false_for_an_idle_tab(monkeypatch):
    """claude exited back to the host shell: the tab is still listed, but the
    session is over and the crash path owns it."""
    herdr_fake(monkeypatch, [(("tab", "list"), 0, TABS),
                             (("pane", "list"), 0, PANES), AT_PROMPT])
    assert Sessions().is_alive("acme", 42) is False


def test_is_alive_false_for_a_tab_restored_after_a_server_restart(monkeypatch):
    """A herdr restart restores every tab as a bare shell wearing its old
    label, so mere existence cannot mean alive: Sessions must consume
    Tab.alive. The restore mechanism itself is covered by
    test_herdr.test_tab_restored_after_server_restart_reads_dead."""
    herdr_fake(monkeypatch, [(("tab", "list"), 0, TABS),
                             (("pane", "list"), 0, PANES),
                             (("pane", "process-info"), 0,
                              _process_info(58952, 58952))])
    assert Sessions().is_alive("acme", 42) is False


def test_is_alive_true_when_process_info_is_unknown(monkeypatch):
    """Fail closed: a false "dead" fires the crash path or a duplicate sweep."""
    herdr_fake(monkeypatch, [(("tab", "list"), 0, TABS),
                             (("pane", "list"), 0, PANES),
                             (("pane", "process-info"), 1,
                              '{"error":{"code":"pane_not_found"},"id":"i"}')])
    assert Sessions().is_alive("acme", 42) is True


# --- spawn_stage / resume ----------------------------------------------------

def test_spawn_stage_writes_the_prompt_and_runs_podman_in_a_new_tab(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake_creating(monkeypatch, calls)
    Sessions().spawn_stage("acme", 42, wt, "PROMPT BODY", "spec",
                           "claude-fable-5")

    assert (tmp_path / ".agent" / "prompt-spec.md").read_text() == "PROMPT BODY"
    # the workspace is created at the worktree on first use ...
    assert ["workspace", "create", "--label", "acme",
            "--cwd", wt, "--no-focus"] in calls
    # ... and HERDR_AGENT rides on the tab, not on the command line.
    assert ["tab", "create", "--workspace", "w1", "--label", "task-acme-42",
            "--cwd", wt, "--env", "HERDR_AGENT=claude", "--no-focus"] in calls
    run = next(c for c in calls if c[:2] == ["pane", "run"])
    assert run[2] == "w1:p3"
    assert run[3] == podman_cmd("acme", 42, wt, "2g", "2", "claude-fable-5",
                                '"$(cat .agent/prompt-spec.md)"')
    assert "HERDR_AGENT" not in run[3]


def test_spawn_stage_closes_an_idle_tab_and_creates_a_fresh_one(tmp_path, monkeypatch):
    """The restored-tab launch path: the label must never carry two tabs, and
    a restored shell must not host a launch that never got the tab env."""
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake_creating(monkeypatch, calls, tabs=TABS, workspaces=WS)
    Sessions().spawn_stage("acme", 42, wt, "P", "spec", "claude-fable-5")

    close_i = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "close"])
    create_i = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "create"])
    assert calls[close_i] == ["tab", "close", "w1:t2"]  # the idle one
    assert close_i < create_i
    assert next(c for c in calls if c[:2] == ["pane", "run"])[2] == "w1:p3"


def test_spawn_stage_on_a_busy_tab_reuses_it(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "run"), 0, "")], calls)
    Sessions().spawn_stage("acme", 42, wt, "P", "plan", "claude-opus-4-8")
    assert not any(c[:2] in (["workspace", "create"], ["tab", "create"],
                             ["tab", "close"]) for c in calls)
    assert calls[-1][:3] == ["pane", "run", "w1:p2"]


def test_spawn_stage_when_the_server_is_down_runs_nothing(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake(monkeypatch, [], calls)
    Sessions().spawn_stage("acme", 42, wt, "P", "spec", "m")  # no raise
    assert not any(c[:2] == ["pane", "run"] for c in calls)


def test_resume_passes_the_quoted_message_and_the_model(tmp_path, monkeypatch):
    wt = _worktree(tmp_path)
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "run"), 0, "")], calls)
    Sessions().resume("acme", 42, wt, 'run said: "failure"', "claude-sonnet-4-6")
    run = next(c for c in calls if c[:2] == ["pane", "run"])
    assert "--model claude-sonnet-4-6" in run[3]
    assert """--continue 'run said: "failure"'""" in run[3]


def test_resume_dry_run_announces_the_session(capsys):
    Sessions(dry_run=True).resume("acme", 42, "/tmp/wt", 'run said: "failure"',
                                  "claude-opus-4-8")
    assert "[dry-run] resume task-acme-42" in capsys.readouterr().out


# --- dry-run -----------------------------------------------------------------

def test_dry_run_touches_no_herdr_and_no_podman(tmp_path, monkeypatch, capsys):
    """Every mutating and every capturing method short-circuits before the
    server. (is_alive has no such branch by design: it is a pure read, and a
    dry-run run must still see which tasks are really live.)"""
    def boom(args):
        raise AssertionError("dry-run must not call herdr")

    def no_podman(*a, **k):
        raise AssertionError("dry-run must not call podman")

    monkeypatch.setattr(herdr, "_run", boom)
    monkeypatch.setattr(sessions.subprocess, "run", no_podman)
    s = Sessions(dry_run=True, state_dir=tmp_path)

    s.spawn_stage("acme", 42, str(tmp_path), "P", "spec", "claude-opus-4-8")
    s.resume("acme", 42, str(tmp_path), "carry on", "claude-opus-4-8")
    assert s.capture_tail("acme", 42) == ""
    assert s.capture_history("acme", 42) == ""
    assert s.idle_seconds("acme", 42) is None
    s.send_text("acme", 42, "code")
    s.end("acme", 42)

    assert not (tmp_path / ".agent").exists()
    assert not (tmp_path / "snapshots").exists()
    out = capsys.readouterr().out
    assert "[dry-run] spawn stage 'spec' on claude-opus-4-8 in session " \
           f"task-acme-42 at {tmp_path}" in out
    assert "[dry-run] send text to task-acme-42" in out
    assert "[dry-run] end session task-acme-42" in out


# --- captures ----------------------------------------------------------------

def test_capture_tail_reads_visible_and_trims_to_the_last_lines(monkeypatch):
    """capture_tail is the dispatcher's classification input: it reads the
    visible pane only and still trims to `lines` (default 25)."""
    body = "\n".join(str(n) for n in range(40)) + "\n"
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, body)], calls)
    out = Sessions().capture_tail("acme", 42)
    assert calls[-1] == ["pane", "read", "w1:p2", "--source", "visible",
                         "--lines", "25"]
    assert out.splitlines() == [str(n) for n in range(15, 40)]


def test_capture_history_reads_recent_unwrapped(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, "line1\nline2\n")], calls)
    out = Sessions().capture_history("acme", 42, lines=500)
    assert out == "line1\nline2"
    assert calls[-1] == ["pane", "read", "w1:p2", "--source", "recent-unwrapped",
                         "--lines", "500"]


def test_captures_are_empty_on_read_failure_dead_tab_or_dead_server(monkeypatch):
    s = Sessions()
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 1,
               '{"error":{"code":"pane_not_found"},"id":"i"}')])
    assert s.capture_tail("acme", 42) == ""
    assert s.capture_history("acme", 42) == ""
    herdr_fake(monkeypatch, [(("tab", "list"), 0, NO_TABS)])
    assert s.capture_tail("acme", 42) == ""
    assert s.capture_history("acme", 42) == ""
    herdr_fake(monkeypatch, [])
    assert s.capture_tail("acme", 42) == ""
    assert s.capture_history("acme", 42) == ""


# --- idle_seconds ------------------------------------------------------------

def _agent(status, seq):
    return (("agent", "get"), 0,
            '{"id":"i","result":{"agent":{"agent_status":"%s","state_change_seq":%d,'
            '"pane_id":"w1:p2"},"type":"agent_info"}}' % (status, seq))


def test_idle_working_is_zero_and_writes_no_sidecar(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, LIVE + [_agent("working", 3)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert Sessions(state_dir=tmp_path).idle_seconds("acme", 42) == 0.0
    assert not (tmp_path / "herdr-status").exists()


def test_idle_new_status_pair_starts_the_clock(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 4)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert Sessions(state_dir=tmp_path).idle_seconds("acme", 42) == 0.0
    side = _json.loads((tmp_path / "herdr-status" / "task-acme-42.json").read_text())
    assert side == {"seq": 4, "status": "idle", "since": 1000.0}


def test_idle_same_pair_accumulates_elapsed(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, LIVE + [_agent("blocked", 4)])
    s = Sessions(state_dir=tmp_path)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert s.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1650.0)
    assert s.idle_seconds("acme", 42) == 650.0


def test_idle_resets_when_seq_or_status_changes(tmp_path, monkeypatch):
    s = Sessions(state_dir=tmp_path)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 4)])
    s.idle_seconds("acme", 42)
    monkeypatch.setattr(sessions.time, "time", lambda: 1500.0)
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 5)])   # new seq, same status
    assert s.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1600.0)
    herdr_fake(monkeypatch, LIVE + [_agent("blocked", 5)])  # same seq, new status
    assert s.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1700.0)
    assert s.idle_seconds("acme", 42) == 100.0


def test_idle_missing_agent_accumulates_like_a_static_screen(tmp_path, monkeypatch):
    # claude exited: the pane is back at the host shell, herdr sees no agent.
    # That is "static" — idle time must accumulate so the stall timer parks it.
    herdr_fake(monkeypatch, LIVE + [(("agent", "get"), 1,
               '{"error":{"code":"agent_not_found","message":"x"},"id":"i"}')])
    s = Sessions(state_dir=tmp_path)
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert s.idle_seconds("acme", 42) == 0.0
    monkeypatch.setattr(sessions.time, "time", lambda: 1900.0)
    assert s.idle_seconds("acme", 42) == 900.0


def test_idle_none_when_dead_server_down_or_no_state_dir(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, [(("tab", "list"), 0, NO_TABS)])
    assert Sessions(state_dir=tmp_path).idle_seconds("acme", 42) is None
    herdr_fake(monkeypatch, LIVE + [(("agent", "get"), 1,
               '{"error":{"code":"server_not_running"},"id":"i"}')])
    assert Sessions(state_dir=tmp_path).idle_seconds("acme", 42) is None
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    assert Sessions().idle_seconds("acme", 42) is None


def test_idle_dry_run_is_none():
    assert Sessions(dry_run=True).idle_seconds("acme", 42) is None


def test_idle_none_on_sidecar_write_failure_never_a_stale_number(tmp_path, monkeypatch):
    (tmp_path / "herdr-status").write_text("not a dir")  # mkdir/write -> OSError
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    assert Sessions(state_dir=tmp_path).idle_seconds("acme", 42) is None


def test_idle_corrupt_sidecar_restarts_the_clock(tmp_path, monkeypatch):
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text("{garbage")
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert Sessions(state_dir=tmp_path).idle_seconds("acme", 42) == 0.0
    assert _json.loads(side.read_text())["since"] == 1000.0


def test_idle_clamps_clock_skew_to_zero(tmp_path, monkeypatch):
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text(_json.dumps({"seq": 1, "status": "idle", "since": 2000.0}))
    herdr_fake(monkeypatch, LIVE + [_agent("idle", 1)])
    monkeypatch.setattr(sessions.time, "time", lambda: 1000.0)
    assert Sessions(state_dir=tmp_path).idle_seconds("acme", 42) == 0.0


def test_forget_status_removes_the_sidecar(tmp_path):
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text("{}")
    Sessions(state_dir=tmp_path).forget_status("acme", 42)
    assert not side.exists()
    Sessions(state_dir=tmp_path).forget_status("acme", 42)  # idempotent
    Sessions().forget_status("acme", 42)                    # no state dir


# --- send_text ---------------------------------------------------------------

def test_send_text_sends_the_literal_text_then_enter(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "send-text"), 0, ""),
                                    (("pane", "send-keys"), 0, "")], calls)
    Sessions().send_text("acme", 42, "abc#123-code")
    assert calls[-2:] == [["pane", "send-text", "w1:p2", "abc#123-code"],
                          ["pane", "send-keys", "w1:p2", "enter"]]


def test_send_text_is_a_noop_when_the_tab_is_gone(monkeypatch):
    calls = []
    herdr_fake(monkeypatch, [(("tab", "list"), 0, NO_TABS)], calls)
    Sessions().send_text("acme", 42, "code")
    assert not any(c[0] == "pane" and c[1].startswith("send") for c in calls)


# --- end ---------------------------------------------------------------------

def test_end_snapshots_then_closes_the_tab_and_removes_both_containers(tmp_path, monkeypatch):
    side = tmp_path / "herdr-status" / "task-acme-42.json"
    side.parent.mkdir()
    side.write_text("{}")
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, "last words\n"),
                                    (("tab", "close"), 0, CLOSE_OK)], calls)
    podman = []
    _fake_podman(monkeypatch, podman)

    Sessions(state_dir=tmp_path).end("acme", 42)

    assert (tmp_path / "snapshots" / "task-acme-42.txt").read_text() == "last words"
    read_i = next(i for i, c in enumerate(calls) if c[:2] == ["pane", "read"])
    close_i = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "close"])
    assert read_i < close_i
    assert calls[close_i] == ["tab", "close", "w1:t2"]
    assert podman == [["podman", "rm", "-f", "task-acme-42"],
                      ["podman", "rm", "-f", "task-42"]]
    assert not side.exists()


def test_end_on_a_dead_task_still_removes_both_container_names(monkeypatch):
    """Containers created before the (target, issue) rekey are named
    task-<issue>; nothing ever renames them, so a wedged one would leak
    until reboot if end() reached for one name only."""
    calls = []
    herdr_fake(monkeypatch, [(("tab", "list"), 0, NO_TABS)], calls)
    podman = []
    _fake_podman(monkeypatch, podman)
    Sessions().end("acme", 42)
    assert not any(c[:2] == ["tab", "close"] for c in calls)
    assert podman == [["podman", "rm", "-f", "task-acme-42"],
                      ["podman", "rm", "-f", "task-42"]]


def test_end_empty_capture_keeps_the_existing_snapshot(tmp_path, monkeypatch):
    # end() runs again on already-dead sessions (e.g. _resume_woken ends
    # before resuming) — an empty capture must not truncate the park's snapshot.
    snap = tmp_path / "snapshots" / "task-acme-42.txt"
    snap.parent.mkdir(parents=True)
    snap.write_text("the parked question")
    herdr_fake(monkeypatch, [(("tab", "list"), 0, NO_TABS)])
    _fake_podman(monkeypatch)
    Sessions(state_dir=tmp_path).end("acme", 42)
    assert snap.read_text() == "the parked question"


def test_end_without_state_dir_writes_no_snapshot(tmp_path, monkeypatch):
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, "last words\n"),
                                    (("tab", "close"), 0, CLOSE_OK)])
    _fake_podman(monkeypatch)
    Sessions().end("acme", 42)
    assert not (tmp_path / "snapshots").exists()


def test_end_snapshot_write_failure_still_closes_the_tab(tmp_path, monkeypatch):
    calls = []
    herdr_fake(monkeypatch, LIVE + [(("pane", "read"), 0, "last words\n"),
                                    (("tab", "close"), 0, CLOSE_OK)], calls)
    _fake_podman(monkeypatch)
    (tmp_path / "snapshots").write_text("not a dir")  # mkdir/write -> OSError
    Sessions(state_dir=tmp_path).end("acme", 42)
    assert ["tab", "close", "w1:t2"] in calls
