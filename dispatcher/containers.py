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


def session_cmd(name: str, worktree: str, memory: str, cpus: str, model: str,
                claude_args: str) -> str:
    clone = clone_root(worktree)
    home = str(Path.home())
    return (
        f"podman run --rm -it --name {name} "
        f"--memory {memory} --cpus {cpus} "
        # Without this, Claude Code keeps onboarding/trust state in
        # /root/.claude.json — a SIBLING of the claude-home mount — so every
        # container boots as a fresh install and stalls on the first-run
        # wizard with nobody attached. CLAUDE_CONFIG_DIR moves all of it
        # inside the mounted claude-home.
        f"-e CLAUDE_CONFIG_DIR=/root/.claude "
        f"-v {worktree}:{worktree} -w {worktree} "
        f"-v {clone}:{clone} "
        f"-v {_state_dir()}/claude-home:/root/.claude "
        f"-v {home}/.config/gh:/root/.config/gh:ro "
        f"-v {home}/.gitconfig:/root/.gitconfig:ro "
        # auto: the classifier approves routine actions and stops only for
        # genuinely risky ones — the stop then flows into the park/resume
        # path (Stop hook → waitd → Telegram). acceptEdits still asked for
        # every non-edit action, which no one is attached to answer.
        #
        # --remote-control <name>: every box session is reachable from
        # claude.ai / the Claude app, named after its task (task-<N>) so it
        # is identifiable there. Remote Control is interactive-only (the
        # headless -p keepalive cannot and need not use it) and needs the
        # claude-home OAuth login, which the mounted store provides. It is a
        # session-config flag, orthogonal to --continue on the resume path.
        f"{image()} claude --remote-control {name} "
        f"--permission-mode auto --model {model} {claude_args}"
    )


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
