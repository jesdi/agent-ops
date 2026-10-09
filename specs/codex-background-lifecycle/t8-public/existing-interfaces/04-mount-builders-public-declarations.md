# Existing mount-producing builders and result artifacts

This supplement declares existing interfaces. T8 implementation and genuine native
actual-launch verification remain required; settled behavior is in the
[presentation contract](../../runtime-presentation-contract.md).


```python
def podman_cmd(target: str, issue: int, worktree: str, memory: str, cpus: str, model: str, args: str, effort: str='', runtime: Runtime | None=None, second: Entry | None=None, launch_env: dict | None=None, *, state_dir: str | Path | None=None) -> str:
    ...
```

```python
def clone_root(worktree: str) -> str:
    ...

def task_branch(worktree: str) -> str:
    ...

def image() -> str:
    ...

def session_cmd(name: str, worktree: str, memory: str, cpus: str, model: str, args: str, effort: str='', runtime: Runtime | None=None, second: Entry | None=None, launch_env: dict | None=None, *, state_dir: str | Path | None=None) -> str:
    ...

def triage_cmd(name: str, clone: str, triage_dir: str, memory: str, cpus: str, model: str, prompt_path: str, effort: str='') -> list[str]:
    ...

def setup_cmd(name: str, worktree: str, setup: str) -> list[str]:
    ...
```

```python
class Runtime:
    cli: str

    home: str

    mount: str

    home_var: str

    env: tuple[str, ...]

    herdr_agent: str

    launch_args: Callable[[str, str, str, str], str]

    resume_args: Callable[[str], str]

    headless_args: Callable[[str, str, str], str]

    package: str = ''

    @property
    def binary(self) -> str:
        ...

    @property
    def binary_mount(self) -> str:
        ...

    def launch(self, name: str, worktree: str, model: str, effort: str) -> str:
        ...

    def headless(self, prompt: str, model: str, effort: str) -> str:
        ...

    def resume(self, session_id: str, message: str) -> str:
        ...

    def resume_cmd(self, session_id: str, message: str='') -> str:
        ...

def runtime_for(model_id: str) -> Runtime:
    ...
```

The two session builders return a complete shell command string. Setup and triage builders return complete argv lists. These are the genuine builder results to inspect/use, rather than reconstructed commands. `model` selects a runtime by provider/model; optional `runtime` is the already selected Runtime. Optional `second:Entry` grants that entry's provider mounts; triage/setup have no secondary-provider argument. `launch_env` is a mapping of launch environment values. A worktree is a genuine git worktree whose `.git` pointer identifies its clone; `.agent/task.json` includes the task `branch` string used by the builder.

`AGENT_OPS_SESSION_IMAGE` selects all session images (default `agent-ops-session`). `AGENT_OPS_COMMAND_WRAPPER` is the existing optional executable/arguments prefix for session and triage commands; setup has no such prefix. Explicit `state_dir` selects session/podman state; setup/triage select `AGENT_OPS_STATE_DIR` or `$HOME/agent-ops-state`. Executable lookup uses the actual environment/PATH. The wrapper is an external command boundary, not a provider/controller replacement API.

Existing mount artifact categories:

| Builder | Host inputs and container destinations |
| --- | --- |
| session/podman | clone/worktree writable at their existing absolute paths; gh config at `/root/.config/gh` and gitconfig at `/root/.gitconfig` read-only; selected primary/secondary provider homes and native installs; `<state_dir>/wait` at the selected wait directory read-only; Codex supervisor source/dependencies read-only at `/opt/agent-ops` and `/opt/agent-ops-deps` |
| triage | clone read-only at its existing path; selected triage directory writable at `/triage`; gh/gitconfig read-only; selected primary provider home/install |
| setup | clone/worktree writable at existing paths; named cache volumes `agent-ops-npm-cache:/root/.npm` and `agent-ops-xdg-cache:/root/.cache` |

The existing Claude runtime passes through `CLAUDE_CODE_OAUTH_TOKEN` and sets `DISABLE_AUTOUPDATER=1`; Codex has no runtime credential passthrough environment tuple. The native no-auth fixture prerequisite remains separate from these production declarations.

Provider home declarations: `openai` → `<state_dir>/codex-home`, `/root/.codex`, `CODEX_HOME`; `anthropic` → `<state_dir>/claude-home`, `/root/.claude`, `CLAUDE_CONFIG_DIR`. Native install selection starts at `$HOME/.local/bin/<cli>` and resolves the genuine install. Codex requires its full package and `codex-package.json`, mounted read-only at `/opt/codex`; Claude's single resolved binary is mounted read-only at `/usr/local/bin/claude`. Codex session source/dependency paths are derived from the imported installed module/dependency locations, not a caller-specified source mount. All actual emitted canonical sources, aliases, access modes and any external wrapper additions remain subject to the existing runtime-store/token exclusion policy; this list does not authorize additional mounts.
