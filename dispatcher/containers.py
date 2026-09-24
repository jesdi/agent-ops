"""Podman command construction shared by session runs (sessions.py) and
one-shot setup runs (workspace.py). Both mount the worktree AND the main
clone at their host paths — a worktree's .git is a file pointing into
<clone>/.git/worktrees/<name>, so git inside the container needs both."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from dispatcher.models import bare_model_id, split_model_id
from dispatcher.runtimes import CLAUDE, Runtime, runtime_for


def clone_root(worktree: str) -> str:
    gitdir = (Path(worktree) / ".git").read_text().split("gitdir:", 1)[1].strip()
    return str(Path(gitdir).parents[2])


def task_branch(worktree: str) -> str:
    """The branch create_workspace recorded in .agent/task.json — injected
    into the session as AGENT_OPS_TASK_BRANCH for the guardrail's lease-push
    exception. "" for a worktree provisioned before the field existed."""
    try:
        d = json.loads((Path(worktree) / ".agent" / "task.json").read_text())
    except (OSError, ValueError):
        return ""
    return str(d.get("branch") or "")


def image() -> str:
    return os.environ.get("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")


def _state_dir() -> str:
    return os.environ.get("AGENT_OPS_STATE_DIR",
                          str(Path.home() / "agent-ops-state"))


def _host_binary(runtime: Runtime) -> list[str]:
    """Run the host's native CLI inside the container, read-only, so the
    box has one CLI at one version (the image used to npm-install its
    own, which drifted behind the auto-updating host install). Resolved at
    spawn: a running container keeps its version even after the host
    updater moves on."""
    binary = os.path.realpath(Path.home() / runtime.binary)
    return ["-v", f"{binary}:{runtime.binary_mount}:ro"]


def _runtime_args(runtime: Runtime) -> list[str]:
    """The runtime's container flags: its home (mounted and pointed at), its
    env, and its host binary. Every container that runs a CLI takes these."""
    return ["-e", f"{runtime.home_var}={runtime.mount}",
            "-v", f"{_state_dir()}/{runtime.home}:{runtime.mount}",
            *(a for e in runtime.env for a in ("-e", e)),
            *_host_binary(runtime)]


def _wrapper() -> list[str]:
    """Optional deployment-owned executable that prepares the session environment."""
    path = os.environ.get("AGENT_OPS_COMMAND_WRAPPER", "")
    return [path] if path else []


def session_cmd(name: str, worktree: str, memory: str, cpus: str, model: str,
                args: str, effort: str = "", runtime: Runtime | None = None) -> str:
    """The session's shell command, on the model's runtime. A caller that
    already resolved it (Sessions._launch) passes it; otherwise it is
    resolved here, and an unknown provider raises before anything runs."""
    runtime = runtime or runtime_for(model)
    clone = clone_root(worktree)
    branch = task_branch(worktree)
    branch_env = f"-e AGENT_OPS_TASK_BRANCH={shlex.quote(branch)} " if branch else ""
    home = str(Path.home())
    # podman errors on a missing bind source; waitd only creates the dir
    # when it (re)starts with the new socket path, which a spawn can race
    # right after deploy. Best-effort: if the dir really can't exist,
    # podman fails loudly on the mount anyway.
    try:
        Path(_state_dir(), "wait").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return (
        f"{shlex.join([*_wrapper(), 'podman'])} run --rm -it --name {name} "
        f"--memory {memory} --cpus {cpus} "
        f"{shlex.join(_runtime_args(runtime))} "
        # The Stop hook fires inside the container and resolves waitd's
        # socket from AGENT_OPS_STATE_DIR — without the wait-dir mount its
        # curl dies against a nonexistent path and the `|| true` swallows
        # it, so waiting parks only ever happened via the stall timer.
        # Mount only the wait dir: the state dir also holds op-token.env.
        f"-e AGENT_OPS_STATE_DIR={_state_dir()} "
        f"{branch_env}"
        f"-v {_state_dir()}/wait:{_state_dir()}/wait "
        f"-v {worktree}:{worktree} -w {worktree} "
        f"-v {clone}:{clone} "
        f"-v {home}/.config/gh:/root/.config/gh:ro "
        f"-v {home}/.gitconfig:/root/.gitconfig:ro "
        f"{image()} {runtime.launch(name, worktree, bare_model_id(model), effort)}"
        f" {args}"
    )


def triage_cmd(name: str, clone: str, triage_dir: str, memory: str,
               cpus: str, model: str, prompt_path: str, effort: str = "") -> list[str]:
    """Headless read-only triage session: argv for subprocess.run (no pane,
    no -it). The clone is :ro — the session decides, it never writes; its
    only writable surface is /triage, where the prompt is read from and the
    decisions file lands.

    prompt_path is the prompt file's path *inside* the container (under
    /triage) — the prompt itself is never an element of this argv. Linux caps
    a single argv string at MAX_ARG_STRLEN (128 KiB) regardless of total
    ARG_MAX, and a busy repo's context blob passed inline made subprocess.run
    raise E2BIG (not SweepError), so the repo reported FAILED, its cursor never
    advanced, and the same oversized window retried every morning forever.

    So the container runs `claude -p "$(cat …)"` under a shell — the same file
    + command-substitution idiom as Sessions.spawn_stage. That keeps the
    dispatcher's own execve small; the container's own execve is kept under the
    same 128 KiB ceiling by triage_prefetch's context budget, which is measured
    on exactly the serialization that lands in the file. Only the shell line is
    composed; the podman argv stays a list so its shape stays assertable.

    Triage is Claude-only (parse_policy rejects other providers in `triage:`);
    a non-anthropic model here is a caller bug, refused before anything runs."""
    provider = split_model_id(model)[0]
    if provider != "anthropic":
        raise ValueError(f"triage runs on Claude only; got {provider!r} model {model!r}")
    home = str(Path.home())
    claude = (f"claude -p \"$(cat {shlex.quote(prompt_path)})\" "
              f"--permission-mode auto --model {shlex.quote(bare_model_id(model))}"
              + (f" --effort {shlex.quote(effort)}" if effort else ""))
    return [
        *_wrapper(),
        "podman", "run", "--rm", "--name", name,
        "--memory", memory, "--cpus", cpus,
        *_runtime_args(CLAUDE),
        "-v", f"{clone}:{clone}:ro", "-w", clone,
        "-v", f"{home}/.config/gh:/root/.config/gh:ro",
        "-v", f"{home}/.gitconfig:/root/.gitconfig:ro",
        "-v", f"{triage_dir}:/triage",
        image(), "bash", "-c", claude,
    ]


def setup_cmd(name: str, worktree: str, setup: str) -> list[str]:
    clone = clone_root(worktree)
    return [
        "podman", "run", "--rm", "--name", name,
        "-v", f"{worktree}:{worktree}", "-w", worktree,
        "-v", f"{clone}:{clone}",
        "-v", "agent-ops-npm-cache:/root/.npm",
        "-v", "agent-ops-xdg-cache:/root/.cache",
        image(),
    ] + shlex.split(setup)
