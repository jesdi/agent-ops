"""What differs between session CLIs, one record per provider: the home the
CLI keeps its state in, its env, its host binary, herdr's agent name, and
how it launches and resumes. Pure data plus string builders; no I/O. The
launcher (containers, sessions) reaches every provider-specific bit through
`runtime_for(model_id)`. See docs/specs/2026-09-24-codex-runtime-design.md."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from dispatcher.models import EFFORTS, split_model_id


@dataclass(frozen=True)
class Runtime:
    home: str               # state-dir subdirectory mounted as the CLI's home
    mount: str              # where that home lands inside the container
    env: tuple[str, ...]    # -e flags; env[0] points the CLI at `mount`
    herdr_agent: str        # herdr's hint for the agent behind the podman wrapper
    binary: str             # host install under $HOME, mounted :ro at /usr/local/bin/<name>
    efforts: tuple[str, ...]
    launch: Callable[[str, str, str, str], str]  # (name, worktree, bare model, effort) -> command
    resume: Callable[[str], str]                 # (quoted message) -> trailing args


CLAUDE = Runtime(
    home="claude-home",
    mount="/root/.claude",
    env=("CLAUDE_CONFIG_DIR=/root/.claude", "CLAUDE_CODE_OAUTH_TOKEN"),
    herdr_agent="claude",
    binary=".local/bin/claude",
    efforts=EFFORTS,
    # auto: the classifier approves routine actions and stops only for
    # genuinely risky ones — the stop then flows into the park/resume path
    # (Stop hook → waitd → Telegram). acceptEdits still asked for every
    # non-edit action, which no one is attached to answer.
    #
    # --remote-control <name>: every box session is reachable from claude.ai
    # / the Claude app, named after its task (task-<target>-<issue>) so it is
    # identifiable there. Remote Control is interactive-only (the headless -p
    # keepalive cannot and need not use it) and needs the claude-home OAuth
    # login, which the mounted store provides. It is a session-config flag,
    # orthogonal to --continue on the resume path.
    launch=lambda name, worktree, model, effort: (
        f"claude --remote-control {name} --permission-mode auto --model {model}"
        f"{' --effort ' + effort if effort else ''}"),
    resume=lambda message: f"--continue {message}",
)

RUNTIMES = {"anthropic": CLAUDE}


def runtime_for(model_id: str) -> Runtime:
    provider = split_model_id(model_id)[0]
    if provider not in RUNTIMES:
        raise ValueError(f"no runtime for provider {provider!r} (model {model_id!r}); "
                         f"known: {sorted(RUNTIMES)}")
    return RUNTIMES[provider]
