"""Podman command construction shared by session runs (sessions.py) and
one-shot setup runs (workspace.py). Both mount the worktree AND the main
clone at their host paths — a worktree's .git is a file pointing into
<clone>/.git/worktrees/<name>, so git inside the container needs both."""
from __future__ import annotations

import os
import shlex
from pathlib import Path


def clone_root(worktree: str) -> str:
    gitdir = (Path(worktree) / ".git").read_text().split("gitdir:", 1)[1].strip()
    return str(Path(gitdir).parents[2])


def image() -> str:
    return os.environ.get("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")


def _state_dir() -> str:
    return os.environ.get("AGENT_OPS_STATE_DIR",
                          str(Path.home() / "agent-ops-state"))


def _wrapper() -> str:
    """with-claude-token.sh resolves the long-lived OAuth token from 1P at
    spawn time and execs podman with it exported — the secret is never
    persisted on the box and never appears in argv or the pane env. Bare
    `-e CLAUDE_CODE_OAUTH_TOKEN` forwards it into the container (podman
    omits an unset passthrough var, so a box without the token degrades to
    the shared claude-home store); with it, claude authenticates statically
    and stops competing for the store's single-use refresh token."""
    return str(Path(__file__).resolve().parents[1]
               / "provision" / "with-claude-token.sh")


def session_cmd(name: str, worktree: str, memory: str, cpus: str, model: str,
                claude_args: str) -> str:
    clone = clone_root(worktree)
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
        f"{_wrapper()} podman run --rm -it --name {name} "
        f"--memory {memory} --cpus {cpus} "
        # Without this, Claude Code keeps onboarding/trust state in
        # /root/.claude.json — a SIBLING of the claude-home mount — so every
        # container boots as a fresh install and stalls on the first-run
        # wizard with nobody attached. CLAUDE_CONFIG_DIR moves all of it
        # inside the mounted claude-home.
        f"-e CLAUDE_CONFIG_DIR=/root/.claude "
        # The Stop hook fires inside the container and resolves waitd's
        # socket from AGENT_OPS_STATE_DIR — without the wait-dir mount its
        # curl dies against a nonexistent path and the `|| true` swallows
        # it, so waiting parks only ever happened via the stall timer.
        # Mount only the wait dir: the state dir also holds op-token.env.
        f"-e AGENT_OPS_STATE_DIR={_state_dir()} "
        f"-v {_state_dir()}/wait:{_state_dir()}/wait "
        f"-v {worktree}:{worktree} -w {worktree} "
        f"-v {clone}:{clone} "
        f"-v {_state_dir()}/claude-home:/root/.claude "
        f"-e CLAUDE_CODE_OAUTH_TOKEN "
        f"-v {home}/.config/gh:/root/.config/gh:ro "
        f"-v {home}/.gitconfig:/root/.gitconfig:ro "
        # auto: the classifier approves routine actions and stops only for
        # genuinely risky ones — the stop then flows into the park/resume
        # path (Stop hook → waitd → Telegram). acceptEdits still asked for
        # every non-edit action, which no one is attached to answer.
        #
        # --remote-control <name>: every box session is reachable from
        # claude.ai / the Claude app, named after its task
        # (task-<target>-<issue>) so it is identifiable there. Remote Control
        # is interactive-only (the headless -p keepalive cannot and need not
        # use it) and needs the claude-home OAuth login, which the mounted
        # store provides. It is a session-config flag, orthogonal to
        # --continue on the resume path.
        f"{image()} claude --remote-control {name} "
        f"--permission-mode auto --model {model} {claude_args}"
    )


def triage_cmd(name: str, clone: str, triage_dir: str, memory: str,
               cpus: str, model: str, prompt_path: str) -> list[str]:
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
    composed; the podman argv stays a list so its shape stays assertable."""
    home = str(Path.home())
    claude = (f"claude -p \"$(cat {shlex.quote(prompt_path)})\" "
              f"--permission-mode auto --model {shlex.quote(model)}")
    return [
        _wrapper(),
        "podman", "run", "--rm", "--name", name,
        "--memory", memory, "--cpus", cpus,
        "-e", "CLAUDE_CONFIG_DIR=/root/.claude",
        "-e", "CLAUDE_CODE_OAUTH_TOKEN",
        "-v", f"{clone}:{clone}:ro", "-w", clone,
        "-v", f"{_state_dir()}/claude-home:/root/.claude",
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
