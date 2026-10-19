"""Ownership and application transport regressions from ticket 09 review."""
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dispatcher import sandbox_runtime, workspace
from dispatcher.sandbox import EngineError, SandboxBlocked, TaskSandbox
from dispatcher.sessions import Sessions
from test_task_sandbox_lifecycle import sandbox


def test_prepare_retry_keeps_retained_volume_after_definition_change(tmp_path):
    engine, box = sandbox(tmp_path)
    original = box.prepare("t1")
    box.park("t1")
    (tmp_path / "clone/compose.yaml").write_text("services: {db: {image: other}}")
    restarted = TaskSandbox(engine, box.state_dir, box.requests)
    engine.create = lambda plan: (_ for _ in ()).throw(EngineError("offline"))
    with pytest.raises(EngineError):
        restarted.prepare("t1")
    assert restarted._plan_for("t1") == original
    restarted.cleanup("t1")
    assert engine.removed == ["t1"]


def test_concurrent_prepare_serializes_across_instances_and_cleanup(tmp_path):
    engine, box = sandbox(tmp_path)
    other = TaskSandbox(engine, box.state_dir, box.requests)
    entered, release = threading.Event(), threading.Event()
    observed = []

    def create(plan):
        observed.append(plan)
        entered.set()
        assert release.wait(5)

    engine.create = create
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(box.prepare, "t1")
        assert entered.wait(5)
        second = pool.submit(other.prepare, "t1")
        release.set()
        assert first.result() == second.result()
    assert observed[0] == observed[1]
    assert json.loads(box._plan_path("t1").read_text())["volumes"]
    other.cleanup("t1")
    box.cleanup("t1")
    assert engine.removed == ["t1"]


@pytest.mark.parametrize("limits", [{"memory": "inf"}, {"cpus": "-1"}, {"cpus": "0"}])
def test_invalid_service_caps_are_concrete_blockers(tmp_path, limits):
    _, box = sandbox(tmp_path)
    path = tmp_path / "clone/compose.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["services"]["db"]["deploy"] = {"resources": {"limits": limits}}
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(SandboxBlocked) as blocked:
        box.prepare("t1")
    assert blocked.value.blockers[0].service == "db"


def test_nonfinite_total_or_agent_demand_blocks_allocation(tmp_path):
    from dispatcher.sandbox import Limits
    _, box = sandbox(tmp_path)
    for req in (replace(box.requests["t1"], limits=Limits(-1, 2)),
                replace(box.requests["t1"], agent_demand=Limits(100, float("nan")))):
        box.requests["t1"] = req
        with pytest.raises(SandboxBlocked):
            box.prepare("t1")


def test_project_workspace_setup_session_and_cleanup_use_owned_plan(tmp_path, monkeypatch):
    _, box = sandbox(tmp_path)
    calls = []

    def runtime_call(self, operation, plan, *args):
        calls.append((operation, plan, args))
        if operation == "exec":
            result = subprocess.run(args[-1], shell=True, cwd=plan.checkout,
                                    capture_output=True, text=True)
            return json.dumps({"exit_code": result.returncode, "output": result.stdout})
        return ""

    monkeypatch.setattr(sandbox_runtime.BoxEngine, "_call", runtime_call)
    target = SimpleNamespace(name="p", repo="me/p", clone_path=str(tmp_path / "clone"),
                             worktrees_path=str(tmp_path / "worktrees"), sandbox=True,
                             compose_services=("db",), setup_cmd="touch installed", gate_cmd="false",
                             sandbox_memory=2 << 30, sandbox_cpus=2)
    wt = workspace.create_workspace(target, 12)
    assert (Path(wt) / "installed").exists()
    assert (Path(wt) / ".git").is_dir()
    assert not (Path(target.clone_path) / "installed").exists()
    sessions = Sessions(clones={"p": target.clone_path}, sandbox_clones=[target.clone_path])
    owned, plan = sessions._task_sandbox(target.clone_path, 12, wt)
    assert owned.resume(plan.ref).ready
    from dispatcher.containers import session_cmd
    monkeypatch.setattr("dispatcher.containers._runtime_args", lambda *a: [])
    monkeypatch.setattr("dispatcher.containers._trust_worktree", lambda *a: None)
    monkeypatch.setattr("dispatcher.containers._state_dir", lambda: str(tmp_path / "state"))
    launcher = session_cmd("task-p-12", wt, "2g", "2", "claude-opus-4-8", "prompt",
                           clone=target.clone_path, sandbox_plan=plan)
    assert f"{target.clone_path}:{target.clone_path}" not in launcher
    agent = plan.containers[0]
    assert f"--cgroup-parent {plan.group}" in launcher
    assert f"--network {agent.network}" in launcher
    assert f"--memory {agent.memory_bytes}" in launcher
    assert "session --plan" in sandbox_runtime.session_command(owned, plan.ref, launcher)
    launches = []
    tab = SimpleNamespace(run=lambda command: launches.append(command) or True, close=lambda: None)
    monkeypatch.setattr("dispatcher.herdr.Tab.ensure", lambda *a, **kw: tab)
    sessions._launch("p", 12, wt, "claude-opus-4-8", "prompt")
    assert launches and "session --plan" in launches[0]
    monkeypatch.setattr(sessions, "_tab", lambda *a: tab)
    monkeypatch.setattr("dispatcher.sessions.subprocess.run", lambda *a, **kw: None)
    sessions.end("p", 12)
    assert any(op == "stop" for op, _, _ in calls)
    workspace.remove_workspace(target, wt, "agent/task-12")
    assert not Path(wt).exists()
    assert calls[-1][0] == "remove"


def test_required_sandbox_never_falls_back_to_legacy(tmp_path):
    sessions = Sessions(sandbox_clones=[str(tmp_path / "clone")])
    with pytest.raises(RuntimeError, match="not been prepared"):
        sessions._task_sandbox(str(tmp_path / "clone"), 1)
    assert Sessions()._task_sandbox(str(tmp_path / "clone"), 1) == (None, None)


def test_runtime_transport_fails_closed_and_keeps_secret_values_out_of_errors(tmp_path, monkeypatch):
    _, box = sandbox(tmp_path)
    plan = box.prepare("t1")
    engine = sandbox_runtime.BoxEngine({"PRIVATE": "secret-value"})
    captured = []

    def run(argv, **kwargs):
        captured.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"exit_code": 3, "output": "failed"}')

    monkeypatch.setattr(sandbox_runtime.subprocess, "run", run)
    engine.create(plan)
    engine.start(plan, plan.containers[0].name)
    assert engine.exec(plan.containers[0].name, "false").exit_code == 3
    engine.stop(plan, plan.containers[0].name)
    engine.remove(plan)
    assert json.loads(captured[0][1]["input"])["ref"] == "t1"
    assert captured[0][1]["env"]["PRIVATE"] == "secret-value"
    monkeypatch.setattr(sandbox_runtime.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=1, stdout="secret-value"))
    with pytest.raises(EngineError, match="failed \\(1\\)") as error:
        engine.create(plan)
    assert "secret-value" not in str(error.value)
    monkeypatch.setattr(sandbox_runtime.subprocess, "run", lambda *a, **kw:
                        (_ for _ in ()).throw(FileNotFoundError("helper")))
    with pytest.raises(EngineError, match="unavailable"):
        engine.create(plan)


@pytest.mark.parametrize("fields", [{"compose_services": "db"}, {"compose_services": [1]},
                                   {"sandbox_cpus": "2"}, {"sandbox_cpus": -1},
                                   {"sandbox_memory": float("inf")}])
def test_sandbox_configuration_rejects_invalid_values(fields):
    from dispatcher.config import _sandbox_config
    with pytest.raises(ValueError):
        _sandbox_config(fields)


def test_sandbox_configuration_deduplicates_confirmed_services():
    from dispatcher.config import _sandbox_config
    fields = {"compose_services": ["db", "db"]}
    _sandbox_config(fields)
    assert fields["compose_services"] == ("db",)


def test_service_failure_parks_application_sandbox_and_retains_ownership(tmp_path, monkeypatch):
    _, box = sandbox(tmp_path)
    calls = []
    def call(self, operation, plan, *args):
        calls.append(operation)
        return '{"exit_code": 7, "output": "private-value"}' if operation == "exec" else ""
    monkeypatch.setattr(sandbox_runtime.BoxEngine, "_call", call)
    target = SimpleNamespace(name="p", repo="me/p", clone_path=str(tmp_path / "clone"),
                             compose_services=("db",), setup_cmd="false", gate_cmd="",
                             sandbox_memory=2 << 30, sandbox_cpus=2)
    with pytest.raises(EngineError, match="setup failed") as failure:
        sandbox_runtime.provision(target, 7, {"PROJECT_SECRET": "private-value"})
    assert "private-value" not in str(failure.value)
    assert calls[-1] == "stop"
    owned = sandbox_runtime.for_clone(target.clone_path)
    plan = owned._plan_for(sandbox_runtime.execution_ref(target.clone_path, 7))
    assert plan.volumes and Path(plan.checkout).exists()
    sessions = Sessions()
    with pytest.raises(RuntimeError, match="differs"):
        sessions._task_sandbox(target.clone_path, 7, "/wrong")


def test_application_refs_do_not_collide_across_project_clones(tmp_path):
    left = sandbox_runtime.execution_ref(tmp_path / "analytics", 12)
    right = sandbox_runtime.execution_ref(tmp_path / "billing", 12)
    assert left != right
    from dispatcher.sandbox import _digest
    assert _digest(left) != _digest(right)


def _target(tmp_path, setup=""):
    return SimpleNamespace(name="p", repo="me/p", clone_path=str(tmp_path / "clone"),
                           compose_services=(), setup_cmd=setup, gate_cmd="",
                           sandbox_memory=2 << 30, sandbox_cpus=2)


def test_provision_retry_never_runs_retained_git_hook_on_host(tmp_path, monkeypatch):
    sandbox(tmp_path)
    marker = tmp_path / "host-hook-ran"

    def call(self, operation, plan, *args):
        if operation == "exec":
            hook = Path(plan.checkout) / ".git/hooks/post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
            hook.chmod(0o755)
            return '{"exit_code": 1}'
        return ""

    monkeypatch.setattr(sandbox_runtime.BoxEngine, "_call", call)
    target = _target(tmp_path, "install-dependencies")
    for _ in range(2):
        with pytest.raises(EngineError, match="setup failed"):
            sandbox_runtime.provision(target, 12, {})
    assert not marker.exists(), "provision retry executed task Git hook on the host"


def test_new_provision_uses_remote_main_and_retry_preserves_edits(tmp_path, monkeypatch):
    sandbox(tmp_path)
    clone, remote = tmp_path / "clone", tmp_path / "remote"

    def git(*args, cwd=clone):
        return subprocess.run(["git", *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("branch", "-M", "main")
    git("clone", str(clone), str(remote))
    git("remote", "add", "origin", str(remote))
    (remote / "new-file").write_text("new remote commit")
    git("add", "new-file", cwd=remote)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "new", cwd=remote)
    remote_tip = git("rev-parse", "HEAD", cwd=remote)
    assert git("rev-parse", "HEAD") != remote_tip
    monkeypatch.setattr(sandbox_runtime.BoxEngine, "_call", lambda *a: "")
    checkout = Path(sandbox_runtime.provision(_target(tmp_path), 12, {}))
    assert (checkout / "new-file").read_text() == "new remote commit"
    assert git("rev-parse", "HEAD", cwd=checkout) == remote_tip
    assert git("branch", "--show-current", cwd=checkout) == "agent/task-12"
    (checkout / "f").write_text("retained edit")
    sandbox_runtime.provision(_target(tmp_path), 12, {})
    assert (checkout / "f").read_text() == "retained edit"


def test_restarted_box_engine_starts_and_executes_durable_plan(tmp_path, monkeypatch):
    _, box = sandbox(tmp_path)
    box.requests["t1"] = replace(box.requests["t1"], setup="install", gate="check", run_gate=True)
    plan = box.prepare("t1")
    calls = []

    def call(self, operation, recovered, *args):
        calls.append((operation, recovered, args))
        return '{"exit_code": 0}' if operation == "exec" else ""

    monkeypatch.setattr(sandbox_runtime.BoxEngine, "_call", call)
    restarted = TaskSandbox(sandbox_runtime.BoxEngine(), box.state_dir, box.requests)
    result = restarted.start("t1")
    assert result.ready
    assert [c.command for c in result.commands] == ["install", "check"]
    assert all(recovered == plan for _, recovered, _ in calls)


def test_sandbox_launcher_keeps_logical_credential_and_home_paths(tmp_path, monkeypatch):
    from dispatcher import containers
    _, box = sandbox(tmp_path)
    plan = box.prepare("t1")
    state, logical = tmp_path / "state", "task-p-12"
    token = Path(containers.token_dir(str(state), logical))
    token.mkdir(parents=True, exist_ok=True)
    (token / "token").write_text("first-token")
    runtime_names = []
    monkeypatch.setattr(containers, "_state_dir", lambda: str(state))
    monkeypatch.setattr(containers, "_trust_worktree", lambda *a: None)
    monkeypatch.setattr(containers, "_runtime_args", lambda runtime, name: runtime_names.append(name) or [])
    launcher = containers.session_cmd(logical, plan.checkout, "2g", "2", "claude-opus-4-8",
                                      "prompt", issue=12, scoped_token=True, sandbox_plan=plan)
    assert f"--name {plan.containers[0].name}" in launcher
    assert f"{token}:{containers.TOKEN_DIR}:ro" in launcher
    assert runtime_names == [logical]
