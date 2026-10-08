"""Podman command construction shared by session runs (sessions.py) and
one-shot setup runs (workspace.py). Both mount the worktree AND the main
clone at their host paths — a worktree's .git is a file pointing into
<clone>/.git/worktrees/<name>, so git inside the container needs both. The
clone path is always the target's configured one, handed in by the caller:
that pointer is session-writable and is never read here."""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from dispatcher.models import Entry, bare_model_id
from dispatcher.runtimes import Runtime, runtime_for
from dispatcher.state import read_regular


_TASK_BRANCH = re.compile(r"agent/task-(\d+)")
TASK_JSON_MAX_BYTES = 64 * 1024


def task_branch(worktree: str, name: str) -> str:
    """The branch create_workspace recorded in .agent/task.json, injected
    into the session as AGENT_OPS_TASK_BRANCH for the guardrail's lease-push
    exception. That file is session-writable, so the value is taken only
    when it is exactly the branch the dispatcher gives the task that the
    container `name` ends in (`agent/task-<issue>`). "" otherwise, also for
    a worktree provisioned before the field existed: the guardrail then
    fails closed on lease pushes."""
    raw = read_regular(Path(worktree) / ".agent" / "task.json", TASK_JSON_MAX_BYTES)
    try:
        branch = json.loads(raw).get("branch") if raw is not None else None
    except (ValueError, AttributeError, RecursionError):
        return ""
    m = _TASK_BRANCH.fullmatch(branch) if isinstance(branch, str) else None
    return branch if m and name.endswith(f"-{m.group(1)}") else ""


def _host_path(what: str, path: str) -> str:
    """A host path as one shell word. Refused when it is not absolute or
    holds a character that the Codex launch arguments (a TOML string inside
    a shell word) cannot carry."""
    if not path.startswith("/") or any(c in path for c in "\"'\\\n\r\0"):
        raise ValueError(f"unusable {what} path for a container command: {path!r}")
    return path


def _mount(source: str, dest: str, mode: str = "") -> str:
    """One `-v` flag with its value as one shell word."""
    return "-v " + shlex.quote(f"{source}:{dest}" + (f":{mode}" if mode else ""))


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
    updater moves on. A packaged CLI mounts its whole package: the
    binary's bin/ parent, which must carry the package manifest, so a lone
    executable never mounts whatever directory happens to sit above it."""
    binary = Path(os.path.realpath(Path.home() / runtime.binary))
    if not runtime.package:
        return ["-v", f"{binary}:{runtime.binary_mount}:ro"]
    root = binary.parents[1]
    if not (root / f"{runtime.cli}-package.json").is_file():
        raise RuntimeError(f"{binary} is not a {runtime.cli.capitalize()} package "
                           f"(no {runtime.cli}-package.json in {root}); "
                           f"agent-ops-infra's {runtime.cli}-install.sh installs one")
    return ["-v", f"{root}:{runtime.package}:ro"]


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
                args: str, effort: str = "",
                second: Entry | None = None, clone: str = "") -> str:
    """The session's shell command, resolved from its qualified model ID.
    An unknown provider raises before anything runs.
    A granted `second` model (models.second_model) also gets its runtime's
    home, env and host binary: the mount is the permission.

    The string runs in a host shell, so every value in it is one quoted
    shell word; only `args` is shell text, and its caller quotes it. `clone`
    is the target's configured clone path: nothing here is read from the
    worktree (its `.git` pointer is session-writable) except the task
    branch, which task_branch validates. With no clone none is mounted; the
    launcher refuses to launch without one."""
    runtime = runtime_for(model)
    extra = _runtime_args(runtime_for(second.model_id)) if second else []
    worktree = _host_path("worktree", worktree)
    state = _state_dir()
    branch = task_branch(worktree, name)
    branch_env = f"-e AGENT_OPS_TASK_BRANCH={shlex.quote(branch)} " if branch else ""
    clone_mount = (_mount(_host_path("clone", clone), clone) + " ") if clone else ""
    home = str(Path.home())
    # podman errors on a missing bind source; waitd only creates the dir
    # when it (re)starts with the new socket path, which a spawn can race
    # right after deploy. Best-effort: if the dir really can't exist,
    # podman fails loudly on the mount anyway.
    try:
        Path(state, "wait").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return (
        f"{shlex.join([*_wrapper(), 'podman'])} run --rm -it --name {shlex.quote(name)} "
        f"--memory {shlex.quote(memory)} --cpus {shlex.quote(cpus)} "
        f"{shlex.join(_runtime_args(runtime) + extra)} "
        # The Stop hook fires inside the container and resolves waitd's
        # socket from AGENT_OPS_STATE_DIR — without the wait-dir mount its
        # curl dies against a nonexistent path and the `|| true` swallows
        # it, so waiting parks only ever happened via the stall timer.
        # Mount only the wait dir: the state dir also holds op-token.env.
        f"-e {shlex.quote('AGENT_OPS_STATE_DIR=' + state)} "
        f"{branch_env}"
        f"{_mount(f'{state}/wait', f'{state}/wait')} "
        f"{_mount(worktree, worktree)} -w {shlex.quote(worktree)} "
        f"{clone_mount}"
        f"{_mount(f'{home}/.config/gh', '/root/.config/gh', 'ro')} "
        f"{_mount(f'{home}/.gitconfig', '/root/.gitconfig', 'ro')} "
        f"{shlex.quote(image())} "
        f"{runtime.launch(name, worktree, bare_model_id(model), effort)}"
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

    So the container runs `<cli> … "$(cat …)"` under a shell — the same file
    + command-substitution idiom as Sessions.spawn_stage. That keeps the
    dispatcher's own execve small; the container's own execve is kept under the
    same 128 KiB ceiling by triage_prefetch's context budget, which is measured
    on exactly the serialization that lands in the file. Only the shell line is
    composed; the podman argv stays a list so its shape stays assertable.

    The CLI is the model's runtime run headless (`claude -p`, `codex
    exec`); an unknown provider raises before anything runs."""
    runtime = runtime_for(model)
    home = str(Path.home())
    line = runtime.headless(f"\"$(cat {shlex.quote(prompt_path)})\"",
                            bare_model_id(model), effort)
    return [
        *_wrapper(),
        "podman", "run", "--rm", "--name", name,
        "--memory", memory, "--cpus", cpus,
        *_runtime_args(runtime),
        "-v", f"{clone}:{clone}:ro", "-w", clone,
        "-v", f"{home}/.config/gh:/root/.config/gh:ro",
        "-v", f"{home}/.gitconfig:/root/.gitconfig:ro",
        "-v", f"{triage_dir}:/triage",
        image(), "bash", "-c", line,
    ]


def setup_cmd(name: str, worktree: str, setup: str, clone: str) -> list[str]:
    """One-shot provisioning container: argv, never a shell line. `clone` is
    the target's configured clone path."""
    return [
        "podman", "run", "--rm", "--name", name,
        "-v", f"{worktree}:{worktree}", "-w", worktree,
        "-v", f"{clone}:{clone}",
        "-v", "agent-ops-npm-cache:/root/.npm",
        "-v", "agent-ops-xdg-cache:/root/.cache",
        image(),
    ] + shlex.split(setup)
