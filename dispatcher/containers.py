"""Podman command construction shared by session runs (sessions.py) and
one-shot setup runs (workspace.py). Both mount the worktree AND the main
clone at their host paths — a worktree's .git is a file pointing into
<clone>/.git/worktrees/<name>, so git inside the container needs both."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from dispatcher.models import Entry, bare_model_id
from dispatcher.runtimes import Runtime, SessionLaunch, runtime_for


def _supervisor_mounts(state_dir: Path) -> list[str]:
    import websockets
    source = Path(__file__).resolve().parent.parent
    dependency = Path(websockets.__file__).resolve().parent.parent
    return [*_bind_mount(source, "/opt/agent-ops", state_dir, read_only=True),
            *_bind_mount(dependency, "/opt/agent-ops-deps", state_dir, read_only=True),
            "-e", "PYTHONPATH=/opt/agent-ops:/opt/agent-ops-deps",
            "-e", "PYTHONDONTWRITEBYTECODE=1"]


def clone_root(worktree: str) -> str:
    gitdir = (Path(worktree) / ".git").read_text().split("gitdir:", 1)[1].strip()
    # Git resolves a relative gitdir from the worktree, not the dispatcher's
    # cwd. Keep symlink/.. components until the host source is canonicalized.
    return str((Path(worktree) / gitdir).parents[2])


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


def _state_dir(state_dir: str | Path | None = None) -> Path:
    return Path(state_dir if state_dir is not None else os.environ.get(
        "AGENT_OPS_STATE_DIR", str(Path.home() / "agent-ops-state"))).absolute()


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _canonical_path(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    existing = resolved
    # Non-strict resolve permits missing paths, but also suppresses errors on
    # recent Python versions. Verify the existing prefix strictly so a loop or
    # inaccessible directory cannot be treated as a separated mount source.
    while True:
        try:
            existing.resolve(strict=True)
            return resolved
        except FileNotFoundError:
            existing = existing.parent


def _bind_mount(source: str | Path, destination: str | Path, state_dir: Path,
                *, read_only: bool = False) -> list[str]:
    """Validate host paths before granting any container access, even read-only.

    Resolve existing symlink components on both sides; missing paths still
    reserve private state before the listener creates it. Provider homes are
    shareable siblings of runtime state, never exceptions to this check.
    """
    root = _canonical_path(source)
    for name in ("runtime", "runtime-host-token"):
        private = _canonical_path(state_dir / name)
        if _overlaps(root, private):
            raise ValueError(f"container mount {source} overlaps private runtime state {private}")
    if not read_only and _overlaps(root, _canonical_path(state_dir / "wait")):
        raise ValueError(f"container mount {source} overlaps the read-only wait directory")
    return ["-v", f"{root}:{destination}" + (":ro" if read_only else "")]


def _host_binary(runtime: Runtime, state_dir: Path) -> list[str]:
    """Run the host's native CLI inside the container, read-only, so the
    box has one CLI at one version (the image used to npm-install its
    own, which drifted behind the auto-updating host install). Resolved at
    spawn: a running container keeps its version even after the host
    updater moves on. A packaged CLI mounts its whole package: the
    binary's bin/ parent, which must carry the package manifest, so a lone
    executable never mounts whatever directory happens to sit above it."""
    binary = Path(os.path.realpath(Path.home() / runtime.binary))
    if not runtime.package:
        return _bind_mount(binary, runtime.binary_mount, state_dir, read_only=True)
    root = binary.parents[1]
    if not (root / f"{runtime.cli}-package.json").is_file():
        raise RuntimeError(f"{binary} is not a {runtime.cli.capitalize()} package "
                           f"(no {runtime.cli}-package.json in {root}); "
                           f"agent-ops-infra's {runtime.cli}-install.sh installs one")
    return _bind_mount(root, runtime.package, state_dir, read_only=True)


def _runtime_args(runtime: Runtime, state_dir: Path) -> list[str]:
    """The runtime's container flags: its home (mounted and pointed at), its
    env, and its host binary. Every container that runs a CLI takes these."""
    return ["-e", f"{runtime.home_var}={runtime.mount}",
            *_bind_mount(state_dir / runtime.home, runtime.mount, state_dir),
            *(a for e in runtime.env for a in ("-e", e)),
            *_host_binary(runtime, state_dir)]


def _wrapper() -> list[str]:
    """Optional deployment-owned executable that prepares the session environment."""
    path = os.environ.get("AGENT_OPS_COMMAND_WRAPPER", "")
    return [path] if path else []


def session_cmd(name: str, worktree: str, memory: str, cpus: str, model: str,
                args: str, effort: str = "", runtime: Runtime | None = None,
                second: Entry | None = None, launch_env: dict | None = None,
                *, state_dir: str | Path | None = None,
                plan: SessionLaunch | None = None) -> str:
    """The session's shell command, on the model's runtime. A caller that
    already resolved it (Sessions._launch) passes it; otherwise it is
    resolved here, and an unknown provider raises before anything runs.
    A granted `second` model (models.second_model) also gets its runtime's
    home, env and host binary: the mount is the permission. Explicit state_dir
    selects both client paths and every bind's private-state validation."""
    runtime = runtime or runtime_for(model)
    state = _state_dir(state_dir)
    extra = _runtime_args(runtime_for(second.model_id), state) if second else []
    plan = plan or runtime.session_plan(name, worktree, bare_model_id(model), effort)
    for resource in plan.support_resources:
        extra += {"codex-supervisor": _supervisor_mounts}[resource](state)
    environment = shlex.join([part for key, value in (launch_env or {}).items()
                              for part in ("-e", f"{key}={value}")])
    # Host sources are canonicalized by _bind_mount. Container paths keep
    # useful aliases, but must be absolute before herdr changes directory.
    worktree = str(Path(worktree).absolute())
    clone = str(Path(clone_root(worktree)).absolute())
    branch = task_branch(worktree)
    branch_env = f"-e AGENT_OPS_TASK_BRANCH={shlex.quote(branch)} " if branch else ""
    home = str(Path.home())
    # podman errors on a missing bind source; waitd only creates the dir
    # when it (re)starts with the new socket path, which a spawn can race
    # right after deploy. Best-effort: if the dir really can't exist,
    # podman fails loudly on the mount anyway.
    try:
        (state / "wait").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return (
        f"{shlex.join([*_wrapper(), 'podman'])} run --rm -it --name {name} "
        f"--memory {memory} --cpus {cpus} "
        f"{shlex.join(_runtime_args(runtime, state) + extra)} "
        # The Stop hook fires inside the container and resolves waitd's
        # socket from AGENT_OPS_STATE_DIR — without the wait-dir mount its
        # curl dies against a nonexistent path and the `|| true` swallows
        # it, so waiting parks only ever happened via the stall timer.
        # Mount only the wait dir, read-only: clients connect to its socket
        # but must not replace the listener and intercept host authorization.
        # The state dir also holds op-token.env and stays outside the mount.
        f"-e AGENT_OPS_STATE_DIR={shlex.quote(str(state))} "
        f"{branch_env}{environment} "
        f"{shlex.join(_bind_mount(state / 'wait', state / 'wait', state, read_only=True))} "
        f"{shlex.join(_bind_mount(worktree, worktree, state))} -w {shlex.quote(worktree)} "
        f"{shlex.join(_bind_mount(clone, os.path.normpath(clone), state))} "
        f"{shlex.join(_bind_mount(Path(home) / '.config/gh', '/root/.config/gh', state, read_only=True))} "
        f"{shlex.join(_bind_mount(Path(home) / '.gitconfig', '/root/.gitconfig', state, read_only=True))} "
        f"{image()} {plan.command}"
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
    state = _state_dir()
    clone = str(Path(clone).absolute())
    home = str(Path.home())
    line = runtime.headless(f"\"$(cat {shlex.quote(prompt_path)})\"",
                            bare_model_id(model), effort)
    return [
        *_wrapper(),
        "podman", "run", "--rm", "--name", name,
        "--memory", memory, "--cpus", cpus,
        *_runtime_args(runtime, state),
        *_bind_mount(clone, clone, state, read_only=True), "-w", clone,
        *_bind_mount(Path(home) / ".config/gh", "/root/.config/gh", state, read_only=True),
        *_bind_mount(Path(home) / ".gitconfig", "/root/.gitconfig", state, read_only=True),
        *_bind_mount(triage_dir, "/triage", state),
        image(), "bash", "-c", line,
    ]


def setup_cmd(name: str, worktree: str, setup: str) -> list[str]:
    state = _state_dir()
    worktree = str(Path(worktree).absolute())
    clone = str(Path(clone_root(worktree)).absolute())
    return [
        "podman", "run", "--rm", "--name", name,
        *_bind_mount(worktree, worktree, state), "-w", worktree,
        *_bind_mount(clone, os.path.normpath(clone), state),
        "-v", "agent-ops-npm-cache:/root/.npm",
        "-v", "agent-ops-xdg-cache:/root/.cache",
        image(),
    ] + shlex.split(setup)
