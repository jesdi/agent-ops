"""dispatcher/herdr.py: JSON envelope parsing and the degrade contract of
the herdr socket-CLI adapter. `_run` is faked per test with a table of
argv-prefix -> (returncode, stdout)."""
import subprocess

from dispatcher import herdr
from dispatcher.herdr import _run as real_run


def fake_run(monkeypatch, table, calls=None):
    """table: list of (argv_prefix, returncode, stdout). First prefix match
    wins; no match -> None (the 'no server' degrade)."""
    def _run(args):
        if calls is not None:
            calls.append(args)
        for prefix, rc, out in table:
            if args[:len(prefix)] == list(prefix):
                return subprocess.CompletedProcess(["herdr", *args], rc, out, "")
        return None
    monkeypatch.setattr(herdr, "_run", _run)


TAB_LIST = ('{"id":"cli:tab:list","result":{"tabs":['
            '{"agent_status":"unknown","focused":true,"label":"1","number":1,'
            '"pane_count":1,"tab_id":"w1:t1","workspace_id":"w1"},'
            '{"agent_status":"working","focused":false,"label":"task-acme-42",'
            '"number":2,"pane_count":1,"tab_id":"w1:t2","workspace_id":"w1"},'
            '{"agent_status":"unknown","focused":false,"label":"triage",'
            '"number":1,"pane_count":1,"tab_id":"w2:t1","workspace_id":"w2"}'
            '],"type":"tab_list"}}')
PANE_LIST = ('{"id":"cli:pane:list","result":{"panes":['
             '{"agent_status":"unknown","focused":true,"pane_id":"w1:p1",'
             '"revision":0,"tab_id":"w1:t1","terminal_id":"t1","workspace_id":"w1"},'
             '{"agent_status":"working","agent":"claude","focused":false,'
             '"pane_id":"w1:p2","revision":9,"tab_id":"w1:t2","terminal_id":"t2",'
             '"workspace_id":"w1"}],"type":"pane_list"}}')
WS_LIST = ('{"id":"cli:workspace:list","result":{"type":"workspace_list",'
           '"workspaces":[{"active_tab_id":"w1:t1","agent_status":"unknown",'
           '"focused":true,"label":"acme","number":1,"pane_count":2,'
           '"tab_count":2,"workspace_id":"w1"}]}}')
WS_CREATED = ('{"id":"cli:workspace:create","result":{"root_pane":{"pane_id":"w3:p1",'
              '"tab_id":"w3:t1","workspace_id":"w3","agent_status":"unknown",'
              '"focused":false,"revision":0,"terminal_id":"t"},'
              '"tab":{"label":"1","tab_id":"w3:t1","workspace_id":"w3","number":1,'
              '"focused":false,"pane_count":1,"agent_status":"unknown"},'
              '"type":"workspace_created","workspace":{"label":"beta",'
              '"workspace_id":"w3","number":3,"focused":false,"pane_count":1,'
              '"tab_count":1,"active_tab_id":"w3:t1","agent_status":"unknown"}}}')
TAB_CREATED = ('{"id":"cli:tab:create","result":{"root_pane":{"pane_id":"w1:p3",'
               '"tab_id":"w1:t3","workspace_id":"w1","agent_status":"unknown",'
               '"focused":false,"revision":0,"terminal_id":"t"},'
               '"tab":{"label":"task-acme-7","tab_id":"w1:t3","workspace_id":"w1",'
               '"number":3,"focused":false,"pane_count":1,"agent_status":"unknown"},'
               '"type":"tab_created"}}')
AGENT_INFO = ('{"id":"cli:agent:get","result":{"agent":{"agent":"claude",'
              '"agent_status":"blocked","state_change_seq":7,"pane_id":"w1:p2",'
              '"tab_id":"w1:t2","workspace_id":"w1","terminal_id":"t2",'
              '"focused":false,"revision":12},"type":"agent_info"}}')
AGENT_NOT_FOUND = ('{"error":{"code":"agent_not_found","message":'
                   '"agent target w1:p9 not found"},"id":"cli:agent:get"}')


def test_run_degrades_on_missing_binary_and_timeout(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("herdr")
    monkeypatch.setattr(herdr.subprocess, "run", boom)
    assert real_run(["tab", "list"]) is None

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="herdr", timeout=30)
    monkeypatch.setattr(herdr.subprocess, "run", slow)
    assert real_run(["tab", "list"]) is None


def test_run_warns_once_when_the_binary_is_missing(monkeypatch, capsys):
    """2026-09-04: the dispatcher ran for a day with no herdr on its PATH.
    Every call degraded to "server down" exactly as designed, so nothing
    launched and nothing said why. A missing binary is not a transient
    server hiccup — say so, once per process, where the journal shows it."""
    monkeypatch.setattr(herdr, "_missing_warned", False)

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "herdr")
    monkeypatch.setattr(herdr.subprocess, "run", boom)
    assert real_run(["tab", "list"]) is None
    assert real_run(["pane", "list"]) is None
    err = capsys.readouterr().err
    assert err.count("herdr binary not found") == 1
    assert herdr.binary() in err


def test_binary_prefers_override_then_local_bin_then_path(monkeypatch, tmp_path):
    """systemd user units run with the user manager's PATH, which has no
    ~/.local/bin — where the official installer puts herdr. Same rule as
    provision/sweep-worktrees.sh: AGENT_OPS_HERDR, else ~/.local/bin/herdr,
    else a bare PATH lookup."""
    monkeypatch.setenv("AGENT_OPS_HERDR", "/opt/herdr/bin/herdr")
    assert herdr.binary() == "/opt/herdr/bin/herdr"

    monkeypatch.delenv("AGENT_OPS_HERDR")
    monkeypatch.setattr(herdr.Path, "home", lambda: tmp_path)
    assert herdr.binary() == "herdr"
    local = tmp_path / ".local" / "bin" / "herdr"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    assert herdr.binary() == str(local)


def test_run_invokes_herdr_with_capture_text_and_timeout(monkeypatch):
    seen = {}

    def run(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(herdr.subprocess, "run", run)
    monkeypatch.setenv("AGENT_OPS_HERDR", "/opt/herdr/bin/herdr")
    real_run(["tab", "list"])
    assert seen["args"] == ["/opt/herdr/bin/herdr", "tab", "list"]
    assert seen["kw"] == {"capture_output": True, "text": True,
                          "timeout": herdr.TIMEOUT}


def test_tab_resolves_label_across_workspaces(monkeypatch):
    fake_run(monkeypatch, [(("tab", "list"), 0, TAB_LIST)])
    assert herdr.tab("task-acme-42") == ("w1", "w1:t2")
    assert herdr.tab("triage") == ("w2", "w2:t1")
    assert herdr.tab("task-acme-99") is None


def test_tab_is_none_on_server_error_syntax_error_and_no_server(monkeypatch):
    fake_run(monkeypatch, [(("tab", "list"), 1,
                           '{"error":{"code":"server_not_running","message":"x"},"id":"i"}')])
    assert herdr.tab("task-acme-42") is None
    fake_run(monkeypatch, [(("tab", "list"), 2, "invalid read source: bogus")])
    assert herdr.tab("task-acme-42") is None
    fake_run(monkeypatch, [])
    assert herdr.tab("task-acme-42") is None


def test_root_pane_matches_tab_id_within_workspace(monkeypatch):
    calls = []
    fake_run(monkeypatch, [(("pane", "list"), 0, PANE_LIST)], calls)
    assert herdr.root_pane("w1", "w1:t2") == "w1:p2"
    assert calls == [["pane", "list", "--workspace", "w1"]]
    assert herdr.root_pane("w1", "w1:t9") is None


def test_workspace_and_ensure_workspace(monkeypatch):
    calls = []
    fake_run(monkeypatch, [(("workspace", "list"), 0, WS_LIST),
                           (("workspace", "create"), 0, WS_CREATED)], calls)
    assert herdr.workspace("acme") == "w1"
    assert herdr.workspace("beta") is None
    assert herdr.ensure_workspace("acme", "/repos/acme") == "w1"
    assert not any(c[:2] == ["workspace", "create"] for c in calls)
    assert herdr.ensure_workspace("beta", "/repos/beta") == "w3"
    assert calls[-1] == ["workspace", "create", "--label", "beta",
                         "--cwd", "/repos/beta", "--no-focus"]


def test_create_tab_returns_root_pane_and_forwards_env(monkeypatch):
    calls = []
    fake_run(monkeypatch, [(("tab", "create"), 0, TAB_CREATED)], calls)
    pane = herdr.create_tab("w1", "task-acme-7", "/wt/7",
                            env={"TELEGRAM_BOT_TOKEN": "tok", "X": "1"})
    assert pane == "w1:p3"
    assert calls == [["tab", "create", "--workspace", "w1",
                      "--label", "task-acme-7", "--cwd", "/wt/7",
                      "--env", "TELEGRAM_BOT_TOKEN=tok", "--env", "X=1",
                      "--no-focus"]]
    assert herdr.create_tab("w1", "t", "/wt") == "w1:p3"  # env optional
    fake_run(monkeypatch, [(("tab", "create"), 1, '{"error":{"code":"workspace_not_found"},"id":"i"}')])
    assert herdr.create_tab("w9", "t", "/wt") is None


def test_read_returns_plain_text_and_none_on_failure(monkeypatch):
    calls = []
    fake_run(monkeypatch, [(("pane", "read"), 0, "line1\nline2\n(base) ➜  tmp")], calls)
    assert herdr.read("w1:p2", "visible", 25) == "line1\nline2\n(base) ➜  tmp"
    assert calls == [["pane", "read", "w1:p2", "--source", "visible",
                      "--lines", "25"]]
    fake_run(monkeypatch, [(("pane", "read"), 1, '{"error":{"code":"pane_not_found"},"id":"i"}')])
    assert herdr.read("w1:p9", "recent-unwrapped", 2000) is None
    fake_run(monkeypatch, [])
    assert herdr.read("w1:p2", "visible", 25) is None


def test_mutations_report_exit_status_only(monkeypatch):
    calls = []
    fake_run(monkeypatch, [(("pane", "run"), 0, ""), (("pane", "send-text"), 0, ""),
                           (("pane", "send-keys"), 0, ""),
                           (("tab", "close"), 0, '{"id":"i","result":{"type":"ok"}}')],
             calls)
    assert herdr.run_command("w1:p2", 'HERDR_AGENT=claude podman run "$(cat x)"') is True
    assert herdr.send_text("w1:p2", "abc#123-code") is True
    assert herdr.send_keys("w1:p2", "enter") is True
    assert herdr.close_tab("w1:t2") is True
    assert calls == [
        ["pane", "run", "w1:p2", 'HERDR_AGENT=claude podman run "$(cat x)"'],
        ["pane", "send-text", "w1:p2", "abc#123-code"],
        ["pane", "send-keys", "w1:p2", "enter"],
        ["tab", "close", "w1:t2"],
    ]
    fake_run(monkeypatch, [(("tab", "close"), 1, '{"error":{"code":"tab_not_found"},"id":"i"}')])
    assert herdr.close_tab("w1:t2") is False
    assert herdr.run_command("w1:p2", "x") is False  # no server


def test_agent_state_reads_status_and_seq(monkeypatch):
    fake_run(monkeypatch, [(("agent", "get"), 0, AGENT_INFO)])
    assert herdr.agent_state("w1:p2") == ("blocked", 7)


def test_agent_state_is_none_agent_when_herdr_sees_no_agent(monkeypatch):
    fake_run(monkeypatch, [(("agent", "get"), 1, AGENT_NOT_FOUND)])
    assert herdr.agent_state("w1:p9") == ("none", 0)


def test_agent_state_is_unknown_on_other_failures(monkeypatch):
    fake_run(monkeypatch, [(("agent", "get"), 1,
                           '{"error":{"code":"server_not_running","message":"x"},"id":"i"}')])
    assert herdr.agent_state("w1:p2") is None
    fake_run(monkeypatch, [(("agent", "get"), 2, "error: unexpected argument")])
    assert herdr.agent_state("w1:p2") is None
    fake_run(monkeypatch, [(("agent", "get"), 0, "not json")])
    assert herdr.agent_state("w1:p2") is None
    fake_run(monkeypatch, [])
    assert herdr.agent_state("w1:p2") is None


def test_agent_state_defaults_missing_seq_to_zero(monkeypatch):
    fake_run(monkeypatch, [(("agent", "get"), 0,
                           '{"id":"i","result":{"agent":{"agent_status":"idle",'
                           '"pane_id":"w1:p2"},"type":"agent_info"}}')])
    assert herdr.agent_state("w1:p2") == ("idle", 0)


def _process_info(shell_pid, pgid):
    return ('{"id":"i","result":{"process_info":{"foreground_process_group_id":%d,'
            '"foreground_processes":[{"name":"x","pid":%d}],"pane_id":"w1:p4",'
            '"shell_pid":%d},"type":"pane_process_info"}}' % (pgid, pgid, shell_pid))


def test_pane_busy_compares_foreground_group_with_the_shell(monkeypatch):
    calls = []
    fake_run(monkeypatch, [(("pane", "process-info"), 0, _process_info(58952, 61907))], calls)
    assert herdr.pane_busy("w1:p4") is True
    assert calls == [["pane", "process-info", "--pane", "w1:p4"]]
    fake_run(monkeypatch, [(("pane", "process-info"), 0, _process_info(58952, 58952))])
    assert herdr.pane_busy("w1:p4") is False


def test_pane_busy_is_none_when_unknown(monkeypatch):
    fake_run(monkeypatch, [(("pane", "process-info"), 1, '{"error":{"code":"pane_not_found"},"id":"i"}')])
    assert herdr.pane_busy("w1:p9") is None
    fake_run(monkeypatch, [(("pane", "process-info"), 0, '{"id":"i","result":{"process_info":{"pane_id":"w1:p4"},"type":"pane_process_info"}}')])
    assert herdr.pane_busy("w1:p4") is None
    fake_run(monkeypatch, [])
    assert herdr.pane_busy("w1:p4") is None


# -- Tab tests ----------------------------------------------------------------

_TAB_42_OLD = ('{"id":"cli:tab:list","result":{"tabs":['
               '{"agent_status":"unknown","focused":false,"label":"task-acme-42",'
               '"number":2,"pane_count":1,"tab_id":"w1:t2","workspace_id":"w1"}'
               '],"type":"tab_list"}}')
_PANE_T2 = ('{"id":"cli:pane:list","result":{"panes":['
            '{"agent_status":"unknown","focused":false,"pane_id":"w1:p2",'
            '"revision":0,"tab_id":"w1:t2","terminal_id":"t1","workspace_id":"w1"}'
            '],"type":"pane_list"}}')
_TAB_42_NEW = ('{"id":"cli:tab:list","result":{"tabs":['
               '{"agent_status":"unknown","focused":false,"label":"task-acme-42",'
               '"number":3,"pane_count":1,"tab_id":"w1:t3","workspace_id":"w1"}'
               '],"type":"tab_list"}}')
_PANE_T3 = ('{"id":"cli:pane:list","result":{"panes":['
            '{"agent_status":"unknown","focused":false,"pane_id":"w1:p3",'
            '"revision":0,"tab_id":"w1:t3","terminal_id":"t1","workspace_id":"w1"}'
            '],"type":"pane_list"}}')
_WS_EMPTY = ('{"id":"cli:workspace:list","result":{"type":"workspace_list",'
             '"workspaces":[]}}')
_TAB_EMPTY = '{"id":"cli:tab:list","result":{"tabs":[],"type":"tab_list"}}'
_TAB_CLOSE_OK = '{"id":"cli:tab:close","result":{"type":"ok"}}'
_WS_W1_ACME = ('{"id":"cli:workspace:list","result":{"type":"workspace_list",'
               '"workspaces":[{"active_tab_id":"w1:t1","agent_status":"unknown",'
               '"focused":true,"label":"acme","number":1,"pane_count":1,'
               '"tab_count":1,"workspace_id":"w1"}]}}')
_TAB_CREATED_42 = ('{"id":"cli:tab:create","result":{"root_pane":{"pane_id":"w1:p3",'
                   '"tab_id":"w1:t3","workspace_id":"w1","agent_status":"unknown",'
                   '"focused":false,"revision":0,"terminal_id":"t"},'
                   '"tab":{"label":"task-acme-42","tab_id":"w1:t3","workspace_id":"w1",'
                   '"number":3,"focused":false,"pane_count":1,"agent_status":"unknown"},'
                   '"type":"tab_created"}}')


def test_tab_find_resolves_label_to_workspace_tab_and_root_pane(monkeypatch):
    fake_run(monkeypatch, [(("tab", "list"), 0, _TAB_42_OLD), (("pane", "list"), 0, _PANE_T2)])
    t = herdr.Tab.find("task-acme-42")
    assert t is not None
    assert t.label == "task-acme-42"
    assert t.workspace_id == "w1"
    assert t.tab_id == "w1:t2"
    assert t.pane_id == "w1:p2"

    # absent label -> None
    fake_run(monkeypatch, [(("tab", "list"), 0, _TAB_EMPTY)])
    assert herdr.Tab.find("task-acme-42") is None

    # pane list failure -> None
    fake_run(monkeypatch, [(("tab", "list"), 0, _TAB_42_OLD),
                           (("pane", "list"), 1, '{"error":{"code":"server_error"},"id":"i"}')])
    assert herdr.Tab.find("task-acme-42") is None


def test_tab_alive_requires_a_busy_shell(monkeypatch):
    t = herdr.Tab("task-acme-42", "w1", "w1:t2", "w1:p2")

    # busy: foreground pgid differs from shell_pid -> alive
    fake_run(monkeypatch, [(("pane", "process-info"), 0, _process_info(100, 200))])
    assert t.alive is True

    # at prompt: pgid == shell_pid -> not alive
    fake_run(monkeypatch, [(("pane", "process-info"), 0, _process_info(100, 100))])
    assert t.alive is False

    # process-info fails -> unknown busyness -> fail closed -> alive
    fake_run(monkeypatch, [])
    assert t.alive is True


def test_tab_restored_after_server_restart_reads_dead(monkeypatch):
    """A herdr server restart restores every tab as a fresh shell wearing its
    old label. Tab.find still resolves it, but alive is False because the
    foreground process group equals the shell pid (bare prompt, no command)."""
    fake_run(monkeypatch, [(("tab", "list"), 0, _TAB_42_OLD),
                           (("pane", "list"), 0, _PANE_T2),
                           (("pane", "process-info"), 0, _process_info(58952, 58952))])
    t = herdr.Tab.find("task-acme-42")
    assert t is not None
    assert t.alive is False


def test_tab_ensure_returns_an_existing_busy_tab_without_creating(monkeypatch):
    calls = []
    fake_run(monkeypatch, [(("tab", "list"), 0, _TAB_42_OLD),
                           (("pane", "list"), 0, _PANE_T2),
                           (("pane", "process-info"), 0, _process_info(100, 200))], calls)
    t = herdr.Tab.ensure("acme", "task-acme-42", "/repos/acme")
    assert t is not None
    assert t.tab_id == "w1:t2"
    assert not any(c[:2] == ["tab", "create"] for c in calls)
    assert not any(c[:2] == ["workspace", "create"] for c in calls)
    assert not any(c[:2] == ["tab", "close"] for c in calls)


def test_tab_ensure_closes_an_idle_tab_under_the_label_then_creates(monkeypatch):
    """`tab close <old id>` precedes `tab create`; returned Tab carries new pane id."""
    tab_list_n = 0
    pane_list_n = 0
    calls = []

    def _run(args):
        nonlocal tab_list_n, pane_list_n
        calls.append(args)
        if args[:2] == ["tab", "list"]:
            tab_list_n += 1
            out = _TAB_42_OLD if tab_list_n == 1 else _TAB_42_NEW
            return subprocess.CompletedProcess(["herdr", *args], 0, out, "")
        if args[:2] == ["pane", "list"]:
            pane_list_n += 1
            out = _PANE_T2 if pane_list_n == 1 else _PANE_T3
            return subprocess.CompletedProcess(["herdr", *args], 0, out, "")
        if args[:2] == ["pane", "process-info"]:
            return subprocess.CompletedProcess(["herdr", *args], 0,
                                               _process_info(100, 100), "")  # idle
        if args[:2] == ["tab", "close"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _TAB_CLOSE_OK, "")
        if args[:2] == ["workspace", "list"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _WS_W1_ACME, "")
        if args[:2] == ["tab", "create"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _TAB_CREATED_42, "")
        return None

    monkeypatch.setattr(herdr, "_run", _run)

    t = herdr.Tab.ensure("acme", "task-acme-42", "/repos/acme")
    assert t is not None
    assert t.pane_id == "w1:p3"  # new pane, not the old one

    close_idx = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "close"])
    create_idx = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "create"])
    assert close_idx < create_idx
    assert calls[close_idx] == ["tab", "close", "w1:t2"]


def test_tab_ensure_creates_workspace_on_first_use_and_forwards_env(monkeypatch):
    """`workspace list` (absent) -> workspace create -> tab create with --env K=V."""
    tab_list_n = 0
    calls = []

    def _run(args):
        nonlocal tab_list_n
        calls.append(args)
        if args[:2] == ["tab", "list"]:
            tab_list_n += 1
            out = _TAB_EMPTY if tab_list_n == 1 else _TAB_42_NEW
            return subprocess.CompletedProcess(["herdr", *args], 0, out, "")
        if args[:2] == ["pane", "list"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _PANE_T3, "")
        if args[:2] == ["workspace", "list"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _WS_EMPTY, "")
        if args[:2] == ["workspace", "create"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, WS_CREATED, "")
        if args[:2] == ["tab", "create"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _TAB_CREATED_42, "")
        return None

    monkeypatch.setattr(herdr, "_run", _run)

    t = herdr.Tab.ensure("beta", "task-acme-42", "/repos/acme", env={"K": "V"})
    assert t is not None
    assert t.pane_id == "w1:p3"

    ws_create = next(c for c in calls if c[:2] == ["workspace", "create"])
    assert ws_create == ["workspace", "create", "--label", "beta",
                         "--cwd", "/repos/acme", "--no-focus"]

    tab_create = next(c for c in calls if c[:2] == ["tab", "create"])
    assert "--env" in tab_create
    assert "K=V" in tab_create


def test_tab_ensure_is_none_when_server_down_or_create_fails(monkeypatch):
    # server completely down -> all herdr calls return None
    fake_run(monkeypatch, [])
    assert herdr.Tab.ensure("acme", "my-tab", "/repos") is None

    # workspace exists but tab create fails
    def _run_create_fails(args):
        if args[:2] == ["tab", "list"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _TAB_EMPTY, "")
        if args[:2] == ["workspace", "list"]:
            return subprocess.CompletedProcess(["herdr", *args], 0, _WS_W1_ACME, "")
        if args[:2] == ["tab", "create"]:
            return subprocess.CompletedProcess(["herdr", *args], 1,
                                               '{"error":{"code":"workspace_not_found"},"id":"i"}', "")
        return None

    monkeypatch.setattr(herdr, "_run", _run_create_fails)
    assert herdr.Tab.ensure("acme", "my-tab", "/repos") is None


def test_tab_methods_delegate_to_the_pane_and_tab(monkeypatch):
    calls = []
    fake_run(monkeypatch, [
        (("pane", "run"), 0, ""),
        (("pane", "read"), 0, "some output"),
        (("agent", "get"), 0, AGENT_INFO),
        (("pane", "send-text"), 0, ""),
        (("pane", "send-keys"), 0, ""),
        (("tab", "close"), 0, '{"id":"i","result":{"type":"ok"}}'),
    ], calls)

    t = herdr.Tab("my-tab", "w1", "w1:t1", "w1:p1")

    assert t.run("echo hi") is True
    assert calls[-1] == ["pane", "run", "w1:p1", "echo hi"]

    assert t.read("visible", 25) == "some output"
    assert calls[-1] == ["pane", "read", "w1:p1", "--source", "visible", "--lines", "25"]

    assert t.agent_state() == ("blocked", 7)
    assert calls[-1] == ["agent", "get", "w1:p1"]

    assert t.send_text("hello") is True
    assert calls[-1] == ["pane", "send-text", "w1:p1", "hello"]

    assert t.send_keys("ctrl-c") is True
    assert calls[-1] == ["pane", "send-keys", "w1:p1", "ctrl-c"]

    assert t.close() is True
    assert calls[-1] == ["tab", "close", "w1:t1"]
