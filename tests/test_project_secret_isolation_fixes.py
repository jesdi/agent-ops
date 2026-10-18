"""Regression tests for the ticket 08 review findings: each runtime's home
variable points at its mounted home, a missing shared login is initialised
centrally, and project secrets reach neither herdr's process arguments nor
the history a session presents and persists."""
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from dispatcher import containers, herdr
from dispatcher.runtimes import CLAUDE, CODEX
from dispatcher.sessions import Sessions, session_name
from tests.test_project_secret_isolation_acceptance import (  # noqa: F401 — fixture
    A_SECRET, MODEL, mounts, spawn, world)

# The real adapter, captured before the fixtures that stub it out.
REAL_ENSURE, REAL_RUN = herdr.Tab.__dict__["ensure"], herdr._run
RUNTIMES = [(CLAUDE, "anthropic/claude-opus-5-5"), (CODEX, "openai/gpt-5.5")]


def _env_value(argv: list[str], name: str) -> str:
    values = [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv[:-1])
              if a == "-e" and argv[i + 1].startswith(f"{name}=")]
    assert len(values) == 1, values
    return values[0]


def _commands(model, wt):
    yield shlex.split(containers.session_cmd("task-a-7", wt, "2g", "2", model, "P"))
    yield containers.triage_cmd("triage-a", "/clone", "/tmp/triage", "2g", "2", model, "/triage/p")


@pytest.mark.parametrize("runtime,model", RUNTIMES, ids=["claude", "codex"])
def test_each_runtime_home_var_points_at_its_mounted_home_and_login(tmp_path, monkeypatch, runtime, model):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    shared = tmp_path / runtime.home
    shared.mkdir()
    (shared / runtime.login).write_text("{}")
    for argv in _commands(model, str(tmp_path / "wt")):
        home = _env_value(argv, runtime.home_var)
        dests = {d: s for s, d, _ in mounts(argv)}
        # The CLI reads its history and its login from where they are mounted.
        assert home in dests and Path(dests[home]).parent.parent.name == "task-homes"
        assert dests[f"{home}/{runtime.login}"] == str(shared / runtime.login)


@pytest.mark.parametrize("runtime,model", RUNTIMES, ids=["claude", "codex"])
def test_a_missing_shared_login_is_initialised_and_bound_into_every_task(tmp_path, monkeypatch, runtime, model):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    login = tmp_path / runtime.home / runtime.login
    for name in ("task-a-7", "task-b-9"):
        argv = shlex.split(containers.session_cmd(name, str(tmp_path / name), "2g", "2",
                                                  model, "P"))
        bound = [s for s, d, _ in mounts(argv) if d == f"{runtime.mount}/{runtime.login}"]
        # One shared file, so a login done in either task is the other's too.
        assert bound == [str(login)]
    assert json.loads(login.read_text()) == {}
    assert login.stat().st_mode & 0o777 == 0o600
    # An existing login is never overwritten.
    login.write_text('{"tokens": "LINEAGE"}')
    containers.session_cmd("task-c-1", str(tmp_path / "c"), "2g", "2", model, "P")
    assert login.read_text() == '{"tokens": "LINEAGE"}'


class HerdrProcess:
    """The herdr CLI at the subprocess boundary: every argv it is run with."""

    def __init__(self, monkeypatch, label):
        self.argvs, self.label, self.created = [], label, False
        real = subprocess.run

        def run(argv, *a, **kw):
            if argv and argv[0] == "podman":
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            if argv and argv[0] != herdr.binary():
                return real(argv, *a, **kw)
            self.argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, self.reply(argv[1:]), "")
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(herdr, "_run", REAL_RUN)

    def reply(self, args):
        tabs = [{"label": self.label, "tab_id": "w1:t3", "workspace_id": "w1"}] if self.created else []
        if args[:2] == ["tab", "create"]:
            self.created = True
        result = {("workspace", "list"): {"workspaces": [{"label": "analytics", "workspace_id": "w1"}]},
                  ("tab", "list"): {"tabs": tabs},
                  ("tab", "create"): {"root_pane": {"pane_id": "w1:p3"}},
                  ("pane", "list"): {"panes": [{"pane_id": "w1:p3", "tab_id": "w1:t3"}]},
                  ("pane", "process-info"): {"process_info": {
                      "foreground_process_group_id": 1, "shell_pid": 1}}}.get(tuple(args[:2]), {})
        return json.dumps({"id": "i", "result": result})

    def typed(self):
        return [a[4] for a in self.argvs if a[1:3] == ["pane", "run"]]


def test_tab_environment_values_never_become_herdr_process_arguments(world, monkeypatch):
    monkeypatch.setattr(herdr.Tab, "ensure", REAL_ENSURE)
    proc = HerdrProcess(monkeypatch, session_name("analytics", 7, "me/analytics"))
    world.sessions.spawn_stage("analytics", 7, world.worktree("a7"), "do", "implement",
                               MODEL, repo="me/analytics")
    # The pane's shell loads the values privately before the launch command runs.
    load, launch = proc.typed()
    assert "podman" in launch
    shown = subprocess.run(["sh", "-c", f"{load}; echo \"$REGISTRY_TOKEN|$GH_TOKEN\""],
                           capture_output=True, text=True, check=True).stdout
    registry, gh_token = shown.strip().split("|")
    assert registry == A_SECRET and gh_token.startswith("ghs_")
    assert not os.path.exists(shlex.split(load.split(";")[0])[1])  # removed once read
    # Neither value is ever an argument of a herdr process.
    argv_text = "\n".join(map(" ".join, proc.argvs))
    assert A_SECRET not in argv_text and gh_token not in argv_text
    tab_env = [a for argv in proc.argvs for i, a in enumerate(argv[1:]) if argv[i] == "--env"]
    assert tab_env == ["HERDR_AGENT=claude"]  # herdr's own hint stays tab-level


class PaneWithHistory:
    alive = True

    def __init__(self, text):
        self.text, self.closed = text, False

    def read(self, source, lines):
        return self.text

    def close(self):
        self.closed = True
        return True


def test_end_redacts_project_secrets_from_presented_and_persisted_history(world, monkeypatch):
    spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    pane = PaneWithHistory(f"$ env | grep REGISTRY\nREGISTRY_TOKEN={A_SECRET}\nfailed: {A_SECRET}")
    monkeypatch.setattr(herdr.Tab, "find", staticmethod(lambda label: pane))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    shown = world.sessions.capture_history("analytics", 7, repo="me/analytics")
    tail = world.sessions.capture_tail("analytics", 7, repo="me/analytics")
    assert A_SECRET not in shown + tail and "REGISTRY_TOKEN=[REDACTED]" in shown
    world.sessions.end("analytics", 7, repo="me/analytics")
    snapshot = sorted((world.state / "snapshots").glob("*.txt"))
    assert len(snapshot) == 1 and pane.closed
    text = snapshot[0].read_text()
    assert A_SECRET not in text and text.count("[REDACTED]") == 2


def test_a_restarted_dispatcher_still_redacts_and_withholds_what_it_cannot(world, monkeypatch):
    pane = PaneWithHistory(f"REGISTRY_TOKEN={A_SECRET}")
    monkeypatch.setattr(herdr.Tab, "find", staticmethod(lambda label: pane))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0))
    # No launch in this process: the values come back from the credential service.
    restarted = Sessions(credentials=world.creds, state_dir=world.state)
    assert restarted.capture_history("analytics", 7, repo="me/analytics") == "REGISTRY_TOKEN=[REDACTED]"
    # An execution whose values cannot be known shows and persists nothing.
    del world.creds.executions[session_name("analytics", 7, "me/analytics")]
    blind = Sessions(credentials=world.creds, state_dir=world.state)
    assert blind.capture_history("analytics", 7, repo="me/analytics") == ""
    blind.end("analytics", 7, repo="me/analytics")
    assert not list(world.state.glob("snapshots/*.txt"))


@pytest.mark.parametrize("screen", [
    # A terminal wrapped the value across two rows (trailing padding too).
    f"REGISTRY_TOKEN={A_SECRET[:12]}  \n{A_SECRET[12:]}\n$ next",
    # The read starts on the wrapped row: only the value's tail is on screen.
    f"{A_SECRET[9:]}\n$ next",
    # The read ends while the value is still being printed.
    f"$ env\nREGISTRY_TOKEN={A_SECRET[:15]}",
], ids=["wrapped", "cut-at-top", "cut-at-bottom"])
def test_a_wrapped_or_cut_secret_is_redacted_from_the_visible_tail(world, monkeypatch, screen):
    spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    monkeypatch.setattr(herdr.Tab, "find", staticmethod(lambda label: PaneWithHistory(screen)))
    tail = world.sessions.capture_tail("analytics", 7, repo="me/analytics")
    history = world.sessions.capture_history("analytics", 7, repo="me/analytics")
    for text in (tail, history):
        assert "[REDACTED]" in text
        assert not any(A_SECRET[i:i + 6] in text for i in range(len(A_SECRET) - 5))


def test_the_console_withholds_a_scoped_sessions_live_output_without_the_credential_service(
        world, monkeypatch):
    """The console is never a credential-service client: it cannot redact, so
    a scoped session's live output is withheld behind a fixed placeholder,
    while a legacy session's output is shown as it is."""
    import uvicorn

    import dispatcher.config
    import dispatcher.credential_service as credential_service
    import web.__main__
    import web.app
    import web.sources

    def contacted(*a, **kw):
        raise AssertionError("the console contacted the credential service")
    monkeypatch.setattr(credential_service, "configured_credentials", contacted)
    monkeypatch.setattr(credential_service.CredentialService, "_request", contacted)
    spawn(world, "analytics", 7, "me/analytics", world.worktree("a7"))
    monkeypatch.setattr(world.creds, "project_env", contacted)
    seen = {}
    monkeypatch.setattr(dispatcher.config, "load_config", lambda path: type(
        "Cfg", (), dict(session_memory="2g", session_cpus="2", state_dir=str(world.state)))())
    monkeypatch.setattr(web.sources, "Sources", lambda state, sessions, gh: seen.setdefault("s", sessions))
    monkeypatch.setattr(web.app, "create_app", lambda cfg, sources: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    web.__main__.main(["--config", str(world.tmp / "targets.yaml")])
    console = seen["s"]
    assert console.credentials is None
    monkeypatch.setattr(herdr.Tab, "find", staticmethod(
        lambda label: PaneWithHistory(f"REGISTRY_TOKEN={A_SECRET}")))
    for text in (console.capture_history("analytics", 7, repo="me/analytics"),
                 console.capture_tail("analytics", 7, repo="me/analytics")):
        assert text == "live output hidden: project secrets"
    # A legacy session (never scoped) is shown as it is.
    assert console.capture_history("legacy", 3) == f"REGISTRY_TOKEN={A_SECRET}"
