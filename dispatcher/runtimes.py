"""What differs between session CLIs, one record per provider: the home the
CLI keeps its state in, its env, its host binary, herdr's agent name, and
how it launches and resumes. Pure data plus string builders; no I/O. The
launcher (containers, sessions) reaches every provider-specific bit through
`runtime_for(model_id)`. See docs/specs/2026-09-24-codex-runtime-design.md."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from dispatcher.models import PROVIDER_EFFORTS, split_model_id


@dataclass(frozen=True)
class Runtime:
    home: str               # state-dir subdirectory mounted as the CLI's home
    mount: str              # where that home lands inside the container
    home_var: str           # env var that points the CLI at `mount`
    env: tuple[str, ...]    # extra -e flags: NAME passes the host's value, NAME=v sets it
    herdr_agent: str        # herdr's hint for the agent behind the podman wrapper
    binary: str             # host install under $HOME, mounted :ro at /usr/local/bin/<name>
    efforts: tuple[str, ...]
    launch: Callable[[str, str, str, str], str]  # (name, worktree, bare model, effort) -> command
    resume: Callable[[str], str]                 # (quoted message) -> trailing args


CLAUDE = Runtime(
    home="claude-home",
    mount="/root/.claude",
    # Without CLAUDE_CONFIG_DIR, Claude Code keeps onboarding/trust state in
    # /root/.claude.json — a SIBLING of the claude-home mount — so every
    # container boots as a fresh install and stalls on the first-run wizard
    # with nobody attached. It moves all of it inside the mounted claude-home.
    home_var="CLAUDE_CONFIG_DIR",
    # The binary is the host's to update (see containers._host_binary), so
    # the in-container auto-updater stays off.
    env=("CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER=1"),
    herdr_agent="claude",
    binary=".local/bin/claude",
    efforts=PROVIDER_EFFORTS["anthropic"],
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

CODEX = Runtime(
    home="codex-home",
    mount="/root/.codex",
    home_var="CODEX_HOME",
    env=(),
    herdr_agent="codex",
    binary=".local/bin/codex",
    efforts=PROVIDER_EFFORTS["openai"],
    # No approval prompts and no sandbox: the container is the isolation
    # layer and nobody is attached to answer. `notify` fires on
    # agent-turn-complete — Codex's Stop hook — running the worktree's own
    # stop-hook.sh (it ignores Codex's JSON argument). The trust override
    # pre-empts the first-run trust prompt that would stall an unattended
    # pane; it is an inline table because -c keeps the quotes of a dotted
    # key segment (projects."<wt>".trust_level never matches). Both are
    # per launch because the worktree path is per task.
    # No --remote-control: Codex has no equivalent.
    launch=lambda name, worktree, model, effort: (
        f"codex --model {model}"
        f"{' -c model_reasoning_effort=' + effort if effort else ''}"
        " --dangerously-bypass-approvals-and-sandbox"
        f" -c 'notify=[\"{worktree}/.agent/stop-hook.sh\"]'"
        f" -c 'projects={{\"{worktree}\"={{trust_level=\"trusted\"}}}}'"),
    # The newest Codex session for the cwd; a stage never changes provider,
    # so that is the stage's.
    resume=lambda message: f"resume --last {message}",
)

RUNTIMES = {"anthropic": CLAUDE, "openai": CODEX}


def runtime_for(model_id: str) -> Runtime:
    provider = split_model_id(model_id)[0]
    if provider not in RUNTIMES:
        raise ValueError(f"no runtime for provider {provider!r} (model {model_id!r}); "
                         f"known: {sorted(RUNTIMES)}")
    return RUNTIMES[provider]
