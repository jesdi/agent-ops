"""Podman command construction shared by session runs (sessions.py) and
one-shot setup runs (workspace.py). Both mount the worktree AND the main
clone at their host paths — a worktree's .git is a file pointing into
<clone>/.git/worktrees/<name>, so git inside the container needs both. The
clone path is always the target's configured one, handed in by the caller:
that pointer is session-writable and is never read here."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
from contextlib import suppress
from pathlib import Path

from dispatcher.models import Entry, bare_model_id
from dispatcher.runtimes import Runtime, runtime_for
from dispatcher.state import read_regular


_TASK_BRANCH = re.compile(r"agent/task-(\d+)")
TASK_JSON_MAX_BYTES = 64 * 1024


def _name_word(name: str) -> str:
    """A container name as one shell word; a plain name stays unquoted."""
    return name if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else shlex.quote(name)


def task_branch(worktree: str, name: str, issue: int | None = None) -> str:
    """The branch create_workspace recorded in .agent/task.json, injected
    into the session as AGENT_OPS_TASK_BRANCH for the guardrail's lease-push
    exception. That file is session-writable, so the value is taken only
    when it is exactly the branch the dispatcher gives the launched task
    (`agent/task-<issue>`). The dispatcher passes `issue`; without it the
    issue is read off the end of the container `name`. "" otherwise, also for
    a worktree provisioned before the field existed: the guardrail then
    fails closed on lease pushes."""
    raw = read_regular(Path(worktree) / ".agent" / "task.json", TASK_JSON_MAX_BYTES)
    try:
        branch = json.loads(raw).get("branch") if raw is not None else None
    except (ValueError, AttributeError, RecursionError):
        return ""
    m = _TASK_BRANCH.fullmatch(branch) if isinstance(branch, str) else None
    if not m:
        return ""
    owned = (int(m.group(1)) == issue if issue is not None
             else name.endswith(f"-{m.group(1)}"))
    return branch if owned else ""


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


TOKEN_DIR = "/run/agent-ops"
TOKEN_FILE = f"{TOKEN_DIR}/gh-token"


def token_dir(state: str | Path, name: str) -> str:
    """Host directory holding a scoped session's current App token."""
    return f"{state}/tokens/{name}"


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


def task_home(state: str | Path, name: str, runtime: Runtime) -> Path:
    """The execution's own writable runtime home. Created on first use from
    the runtime's fixed bootstrap seed, never from the shared home; its login
    file is bound in instead (see _runtime_args)."""
    # One path component, whatever the name holds (no "." so never ".."). A
    # name that needed sanitising carries a digest of the raw name, so two
    # different names never share a home.
    safe = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else (
        re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] + "-" + hashlib.sha256(name.encode()).hexdigest()[:16])
    home = Path(state, "task-homes", safe, runtime.home)
    if not home.exists():
        try:  # best-effort like the wait dir: podman fails loudly on a missing source
            home.mkdir(parents=True)
            for fname, content in (runtime.seed or {}).items():
                (home / fname).write_text(content)
        except OSError:
            pass  # the CLI starts from scratch
    return home


def _owns(path: Path, session_id: str, runtime: Runtime) -> bool:
    """A conversation file named exactly for the session (never a substring)."""
    return bool(session_id) and path.is_file() and not path.is_symlink() \
        and path.name != runtime.login \
        and (path.stem == session_id or path.name.endswith(f"-{session_id}.jsonl"))


def _recorded_cwd(path: Path) -> str | None:
    """The working directory the conversation file itself records (the first
    record that carries one, top level or in a `payload`)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                cwd = record.get("cwd") or (payload.get("cwd") if isinstance(payload, dict) else None)
                if cwd:
                    return str(cwd)
    except OSError:
        pass
    return None


def _publish(home: Path, rel: Path, src: Path) -> None:
    """Copy `src` to home/rel through directory descriptors that refuse links
    (the home is task-writable, so a swapped parent never redirects the write),
    into a temporary file linked into place only once complete."""
    flags = os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(home, flags)
    try:
        for part in rel.parent.parts:
            with suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fd)
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        tmp = f".{rel.name}.{secrets.token_hex(6)}.tmp"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(out, "wb") as dest, \
                    open(os.open(src, os.O_RDONLY | os.O_NOFOLLOW), "rb") as inp:
                shutil.copyfileobj(inp, dest)
            os.link(tmp, rel.name, src_dir_fd=fd, dst_dir_fd=fd)  # never replaces
        except FileExistsError:
            pass  # already migrated
        finally:
            with suppress(OSError):
                os.unlink(tmp, dir_fd=fd)
    finally:
        os.close(fd)


def migrate_conversation(state: str | Path, name: str, runtime: Runtime,
                         session_id: str, worktree: str) -> None:
    """Move one conversation that predates task homes from the shared home into
    the task's own: only files whose name carries this session's ID AND that
    record this task's `worktree` as their working directory (a
    session ID a task reported is not proof of ownership; every CLI record of a
    real session names the worktree it ran in), so no sibling history is exposed."""
    shared = Path(state, runtime.home)
    home = task_home(state, name, runtime)
    for src in (p for p in shared.rglob("*") if _owns(p, session_id, runtime)):
        if _recorded_cwd(src) != worktree:  # missing ownership evidence fails closed
            continue
        with suppress(OSError):  # a link in the way: leave the file behind
            _publish(home, src.relative_to(shared), src)


def _shared_login(state: str, runtime: Runtime) -> Path:
    """The runtime's one shared login file, created empty (`{}`, 0600) when
    absent so it is always bound: a login done inside any task then lands in
    it, never in that task's own home. An existing login is never touched. A
    file that cannot be created stays missing, and podman refuses the mount."""
    login = Path(state, runtime.home, runtime.login)
    with suppress(OSError):
        login.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(login, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write("{}")
    return login


def _runtime_args(runtime: Runtime, name: str) -> list[str]:
    """The runtime's container flags: its per-execution home (mounted and
    pointed at), the shared login file bound into it (one refresh lineage,
    never a copy), its env, and its host binary. Every container that runs a
    CLI takes these."""
    state = _state_dir()
    return ["-e", f"{runtime.home_var}={runtime.mount}",
            "-v", f"{task_home(state, name, runtime)}:{runtime.mount}",
            *(["-v", f"{_shared_login(state, runtime)}:{runtime.mount}/{runtime.login}"]
              if runtime.login else []),
            *(a for e in runtime.env for a in ("-e", e)),
            *_host_binary(runtime)]


def _trust_worktree(runtime: Runtime, name: str, worktree: str) -> None:
    """Trust only this launch's worktree in its isolated Claude configuration."""
    if runtime.cli != "claude":
        return
    from dispatcher.workspace import read_worktree_file, write_worktree_file

    home = task_home(_state_dir(), name, runtime)
    if not home.is_dir():
        return  # unavailable mount sources are reported by podman
    raw = read_worktree_file(home.parent, home.name, ".claude.json")
    try:
        config = json.loads(raw) if raw else {}
        if not isinstance(config, dict):
            config = {}
    except ValueError:
        config = {}
    config["hasCompletedOnboarding"] = True
    config["projects"] = {worktree: {"hasTrustDialogAccepted": True}}
    write_worktree_file(home.parent, home.name, ".claude.json", json.dumps(config), mode=0o600)


def _wrapper() -> list[str]:
    """Optional deployment-owned executable that prepares the session environment."""
    path = os.environ.get("AGENT_OPS_COMMAND_WRAPPER", "")
    return [path] if path else []


def credential_commands() -> list[str]:
    """Read-only consumers for the renewable token channel inside a session."""
    scripts = str(Path(__file__).resolve().parent / "credential_commands")
    return ["-v", f"{scripts}:/opt/agent-ops-auth:ro", "-e",
            "PATH=/opt/agent-ops-auth:/usr/local/bin:/usr/bin:/bin"]


def session_cmd(name: str, worktree: str, memory: str, cpus: str, model: str,
                args: str, effort: str = "",
                second: Entry | None = None, clone: str = "",
                issue: int | None = None, scoped_token: bool = False,
                secret_names: tuple[str, ...] = (), sandbox_plan=None) -> str:
    """The session's shell command, resolved from its qualified model ID.
    An unknown provider raises before anything runs.
    A granted `second` model (models.second_model) also gets its runtime's
    home, env and host binary: the mount is the permission.

    The string runs in a host shell, so every value in it is one quoted
    shell word; only `args` is shell text, and its caller quotes it. `clone`
    is the target's configured clone path: nothing here is read from the
    worktree (its `.git` pointer is session-writable) except the task
    branch, which task_branch validates. With no clone none is mounted; the
    launcher refuses to launch without one.

    `scoped_token`: the execution holds a scoped App token (see
    execution_credentials), delivered as GH_TOKEN inherited from the
    launcher's environment (never in argv); the host gh login is not mounted.
    `secret_names`: the project's secret variables, passed by name (`-e NAME`);
    their values live only in the launcher's environment."""
    runtime = runtime_for(model)
    worktree = _host_path("worktree", worktree)
    _trust_worktree(runtime, name, worktree)
    if second:
        _trust_worktree(runtime_for(second.model_id), name, worktree)
    extra = _runtime_args(runtime_for(second.model_id), name) if second else []
    state = _state_dir()
    branch = task_branch(worktree, name, issue)
    branch_env = f"-e AGENT_OPS_TASK_BRANCH={shlex.quote(branch)} " if branch else ""
    sandbox_flags = ""
    container_name = name
    if sandbox_plan is not None:
        agent = next(c for c in sandbox_plan.containers if c.role == "agent")
        container_name, memory, cpus = agent.name, str(agent.memory_bytes), str(agent.cpus)
        clone = ""
        sandbox_flags = shlex.join(["--network", agent.network,
                                    "--cgroup-parent", agent.group]) + " "
        # a multi-repository task: the other checkouts, at the paths the sandbox ran their commands in
        sandbox_flags += "".join(_mount(m.source, m.dest) + " " for m in agent.mounts
                                 if m.dest.startswith("/workspaces/"))
    clone_mount = (_mount(_host_path("clone", clone), clone) + " ") if clone else ""
    home = str(Path.home())
    gh_login = _mount(f'{home}/.config/gh', '/root/.config/gh', 'ro') + " "
    # GH_TOKEN is the launch token; the token directory is the renewable
    # channel (Sessions.renew_tokens rewrites the file before expiry).
    scoped_env = (f"-e GH_TOKEN -e GH_TOKEN_FILE={TOKEN_FILE} "
                  f"{_mount(token_dir(state, name), TOKEN_DIR, 'ro')} "
                  f"{shlex.join(credential_commands())} ")
    # podman errors on a missing bind source; waitd only creates the dir
    # when it (re)starts with the new socket path, which a spawn can race
    # right after deploy. Best-effort: if the dir really can't exist,
    # podman fails loudly on the mount anyway.
    try:
        Path(state, "wait").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    secret_env = "".join(f"-e {shlex.quote(n)} " for n in secret_names)
    return (
        f"{shlex.join([*_wrapper(), 'podman'])} run --rm -it --name {_name_word(container_name)} "
        f"--memory {shlex.quote(memory)} --cpus {shlex.quote(cpus)} {sandbox_flags}"
        f"{shlex.join(_runtime_args(runtime, name) + extra)} "
        # The Stop hook fires inside the container and resolves waitd's
        # socket from AGENT_OPS_STATE_DIR — without the wait-dir mount its
        # curl dies against a nonexistent path and the `|| true` swallows
        # it, so waiting parks only ever happened via the stall timer.
        # Mount only the wait dir: the state dir also holds op-token.env.
        f"-e {shlex.quote('AGENT_OPS_STATE_DIR=' + state)} "
        f"{branch_env}{secret_env}"
        f"{_mount(f'{state}/wait', f'{state}/wait')} "
        f"{_mount(worktree, worktree)} -w {shlex.quote(worktree)} "
        f"{clone_mount}"
        f"{scoped_env if scoped_token else gh_login}"
        f"{_mount(f'{home}/.gitconfig', '/root/.gitconfig', 'ro')} "
        f"{shlex.quote(image())} "
        f"{runtime.launch(name, worktree, bare_model_id(model), effort)}"
        f" {args}"
    )


def triage_cmd(name: str, clone: str, triage_dir: str, memory: str,
               cpus: str, model: str, prompt_path: str, effort: str = "",
               scoped_token: bool = False) -> list[str]:
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
        *_runtime_args(runtime, name),
        "-v", f"{clone}:{clone}:ro", "-w", clone,
        *(["-e", "GH_TOKEN"] if scoped_token
          else ["-v", f"{home}/.config/gh:/root/.config/gh:ro"]),
        "-v", f"{home}/.gitconfig:/root/.gitconfig:ro",
        "-v", f"{triage_dir}:/triage",
        image(), "bash", "-c", line,
    ]


def setup_cmd(name: str, worktree: str, setup: str, clone: str,
              project: str = "", secret_names: tuple[str, ...] = ()) -> list[str]:
    """One-shot provisioning container: argv, never a shell line. `clone` is
    the target's configured clone path. Build caches are per `project`, so one
    project's writable cache is never another's; `secret_names` are passed by
    name only (`-e NAME`), the values come from the launcher's environment."""
    # A digest of the whole key: lossy sanitising alone could merge two projects.
    suffix = "-" + hashlib.sha256(project.encode()).hexdigest()[:16] if project else ""
    return [
        "podman", "run", "--rm", "--name", name,
        *(a for n in secret_names for a in ("-e", n)),
        "-v", f"{worktree}:{worktree}", "-w", worktree,
        "-v", f"{clone}:{clone}",
        "-v", f"agent-ops-npm-cache{suffix}:/root/.npm",
        "-v", f"agent-ops-xdg-cache{suffix}:/root/.cache",
        image(),
    ] + shlex.split(setup)
