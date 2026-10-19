"""Application transport for the box sandbox runtime (provisioned by infra).

The helper consumes a ServicePlan on stdin for lifecycle operations. It must
apply the plan's aggregate resource group before creating any container and
fail closed when unavailable. `session` consumes the durable plan via --plan,
replaces its idle agent with the supplied interactive launcher in the same
resource group, and retains supporting services. No fallback runs on the box.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path

from dispatcher.sandbox import (WORKSPACE, EngineError, ExecResult, Limits, SandboxMember,
                                SandboxRequest, TaskSandbox)


HELPER = "agent-ops-sandbox"


class BoxEngine:
    def __init__(self, env=None):
        self.env = env or {}
        self.plans = {}
        self.helper = os.environ.get("AGENT_OPS_SANDBOX_RUNTIME", HELPER)

    def _call(self, operation, plan, *args):
        try:
            result = subprocess.run(
                [self.helper, operation, *args], input=json.dumps(asdict(plan)),
                capture_output=True, text=True, timeout=1800,
                env={**os.environ, **self.env})
        except (OSError, subprocess.SubprocessError) as error:
            raise EngineError(f"sandbox runtime {operation} unavailable: {error}") from error
        if result.returncode:
            # The runtime's output may contain project secrets.
            raise EngineError(f"sandbox runtime {operation} failed ({result.returncode})")
        return result.stdout

    def create(self, plan):
        self.plans.update({c.name: plan for c in plan.containers})
        self._call("create", plan)

    def start(self, plan, name):
        self._call("start", plan, "--container", name)
        self.plans[name] = plan

    def stop(self, plan, name):
        self._call("stop", plan, "--container", name)

    def remove(self, plan):
        self._call("remove", plan)

    def exec(self, name, command, workdir=WORKSPACE):
        # exec is the one operation returning a command result rather than a
        # runtime exit status. The helper never writes command output to logs.
        extra = () if workdir == WORKSPACE else ("--workdir", workdir)
        result = json.loads(self._call("exec", self.plans[name], "--container", name,
                                       "--command", command, *extra))
        return ExecResult(result["exit_code"], result.get("output", ""))


def for_clone(clone, env=None, requests=None):
    path = Path(clone)
    return TaskSandbox(BoxEngine(env), path.parent / f"{path.name}.sandboxes", requests or {})


def execution_ref(clone, issue):
    return f"{Path(clone).resolve()}#{issue}"


def provision(target, issue, env, members=(), establish_scope=False):
    ref = execution_ref(target.clone_path, issue)
    request = SandboxRequest(target.name, target.repo, target.clone_path,
                             services=tuple(target.compose_services), setup=target.setup_cmd,
                             gate=target.gate_cmd, run_gate=False,
                             branch=f"agent/task-{issue}",
                             limits=Limits(target.sandbox_memory, target.sandbox_cpus),
                             members=tuple(SandboxMember(
                                 m.repo, m.clone_path, tuple(m.compose_services), m.setup_cmd,
                                 m.gate_cmd, branch=f"agent/task-{issue}") for m in members))
    sandbox = for_clone(target.clone_path, env, {ref: request})
    plan = (sandbox.establish_scope(ref) if establish_scope else sandbox.prepare(ref))
    try:
        result = sandbox.start(ref)
        if not result.ready:
            from dispatcher.workspace import redact
            raise EngineError(redact(f"sandbox setup failed: {result.failure}", env))
    except Exception:
        sandbox.park(ref)
        raise
    return plan.checkout


def session_command(sandbox, ref, launcher):
    """The infra helper atomically replaces the idle agent with this session."""
    return shlex.join([getattr(sandbox.engine, "helper", HELPER), "session", "--plan",
                       str(sandbox._plan_path(ref)), "--"]) + " " + launcher


def cleanup(clone, issue):
    """True after owned cleanup; False for a workspace predating sandboxes."""
    sandbox = for_clone(clone)
    ref = execution_ref(clone, issue)
    if not sandbox._plan_path(ref).exists():
        return False
    sandbox.cleanup(ref)
    return True


class BoxSandboxes:
    """The box's task sandboxes: each execution ref belongs to its clone's."""

    @staticmethod
    def _of(ref):
        return for_clone(ref.rpartition("#")[0])

    def owns(self, ref):
        return self._of(ref).owns(ref)

    def park(self, ref):
        self._of(ref).park(ref)

    def resume(self, ref):
        return self._of(ref).resume(ref)

    def cleanup(self, ref):
        self._of(ref).cleanup(ref)
