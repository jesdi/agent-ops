# Existing Sessions, handles and Config inputs

This supplement declares existing interfaces. T8 implementation and genuine native
actual-launch verification remain required; settled behavior is in the
[presentation contract](../../runtime-presentation-contract.md).


```python
def session_name(target: str, issue: int) -> str:
    ...

class Sessions:
    def __init__(self, dry_run: bool=False, memory: str='2g', cpus: str='2', state_dir: str | Path | None=None):
        ...

    def runtime_view(self, target: str, issue: int) -> dict | None:
        ...

    def is_alive(self, target: str, issue: int) -> bool:
        ...

    def spawn_stage(self, target: str, issue: int, worktree: str, prompt: str, stage_name: str, model: str, effort: str='', second: Entry | None=None, *, ticket: str='') -> None:
        ...

    def resume(self, target: str, issue: int, worktree: str, message: str, model: str, effort: str='', second: Entry | None=None, *, session_id: str) -> None:
        ...

    def capture_tail(self, target: str, issue: int, lines: int=25) -> str:
        ...

    def capture_history(self, target: str, issue: int, lines: int=2000) -> str:
        ...

    def idle_seconds(self, target: str, issue: int) -> float | None:
        ...

    def agent_state(self, target: str, issue: int) -> tuple[str, int] | None:
        ...

    def forget_status(self, target: str, issue: int) -> None:
        ...

    def send_text(self, target: str, issue: int, text: str) -> None:
        ...

    def end(self, target: str, issue: int) -> None:
        ...
```

`session_name(target, issue)` returns `task-<target>-<issue>`. The same label identifies the herdr tab and Podman task container; the workspace label is `target`. Spawn/resume return `None`, not a launch/process handle. Real managed spawn/resume requires explicit `state_dir`. Its binding is observable through the existing RuntimeClient/`runtime_view` interface. Spawn prompt artifact is `<worktree>/.agent/prompt-<stage_name>.md`; resume prompt artifact is `.agent/prompt-resume.md`. `resume` requires an explicit native `session_id`. No `Sessions.attach` API exists.

Existing herdr operational handle declarations:

```python
def binary() -> str:
    ...

def workspace(label: str) -> str | None:
    ...

def ensure_workspace(label: str, cwd: str) -> str | None:
    ...

def tab(label: str) -> tuple[str, str] | None:
    ...

def root_pane(workspace_id: str, tab_id: str) -> str | None:
    ...

def create_tab(workspace_id: str, label: str, cwd: str, env: dict[str, str] | None=None) -> str | None:
    ...

def read(pane_id: str, source: str, lines: int) -> str | None:
    ...

def run_command(pane_id: str, command: str) -> bool:
    ...

def send_text(pane_id: str, text: str) -> bool:
    ...

def send_keys(pane_id: str, *keys: str) -> bool:
    ...

def close_tab(tab_id: str) -> bool:
    ...

def pane_busy(pane_id: str) -> bool | None:
    ...

def agent_state(pane_id: str) -> tuple[str, int] | None:
    ...

class Tab:
    label: str

    workspace_id: str

    tab_id: str

    pane_id: str

    @classmethod
    def find(cls, label: str) -> 'Tab | None':
        ...

    @classmethod
    def ensure(cls, workspace_label: str, label: str, cwd: str, env: dict[str, str] | None=None) -> 'Tab | None':
        ...

    @property
    def alive(self) -> bool:
        ...

    def run(self, command: str) -> bool:
        ...

    def read(self, source: str, lines: int) -> str | None:
        ...

    def agent_state(self) -> tuple[str, int] | None:
        ...

    def send_text(self, text: str) -> bool:
        ...

    def send_keys(self, *keys: str) -> bool:
        ...

    def close(self) -> bool:
        ...
```

`Tab` is frozen; IDs are current topology observations, and labels are the supported lookup identity. `AGENT_OPS_HERDR` selects the executable; otherwise the installed `$HOME/.local/bin/herdr`, then `herdr` on PATH, is selected. Existing documented terminal attachment is `herdr --remote box` (matching local/server version); attachment selects the real tab/root in herdr. Exact owned local-server startup/namespace/terminal-select CLI configuration still requires the genuine versioned CLI artifact listed in [native prerequisites](06-native-prerequisites-public-declarations.md); no Python attachment seam is invented.

`capture_history` is pane history, not the task audit boundary. Session snapshot artifact is `<state_dir>/snapshots/<session_name>.txt`; idle sidecar is `<state_dir>/herdr-status/<session_name>.json`, with `seq:int`, `status:str`, `since:number`. `end` returns no handle and may also attempt legacy container name `task-<issue>`; the owned identity inventory must account for that existing name before execution.

Config records (frozen) and loader:

```python
class Target:
    name: str

    repo: str

    clone_path: str

    worktrees_path: str

    rank_cmd: str

    project_number: int

    project_owner: str

    status_field_id: str

    status_ready_option_id: str

    status_in_progress_option_id: str

    setup_cmd: str = ''

    verify_cmd: str = ''

    gate_cmd: str = ''

    boost_field_id: str = ''

    status_done_option_id: str = ''

    status_wont_do_option_id: str = ''

    max_active: int | None = None

    models: ModelPolicy | None = None

class Config:
    state_dir: str

    capacity: int

    session_memory: str

    session_cpus: str

    targets: list[Target]

    infra_repo: str = ''

    models: ModelPolicy = DEFAULT_POLICY

    console_url: str = ''

    stall_after_seconds: int = 600

    background_wait_seconds: int = 10800

    spec_review_grace_minutes: int | None = 15

    done_retention_days: int = 7

    pass_interval_minutes: int = 10

    loop_caps: LoopCaps = LoopCaps()

    pace: PaceConfig = PaceConfig()

def load_config(path: str | Path) -> Config:
    ...

def policy_for(cfg: Config, target: Target) -> ModelPolicy:
    ...

def routed_providers(cfg: Config) -> frozenset[str]:
    ...

def referenced_providers(cfg: Config) -> frozenset[str]:
    ...
```

```python
class Entry:
    provider: str

    model: str

    effort: str = ''

    @property
    def model_id(self) -> str:
        ...

def parse_entry(value: object, context: str) -> Entry:
    ...

class Track:
    name: str

    when: str

    stages: Mapping[str, tuple[Entry, ...]]

class ModelPolicy:
    triage: tuple[Entry, ...]

    untracked: str

    tracks: Mapping[str, Track]

    review_second: str = ''

    def entries(self) -> list[Entry]:
        ...

    def model_ids(self) -> list[str]:
        ...

    def gate_entry(self) -> Entry:
        ...

def parse_policy(raw: dict | None) -> ModelPolicy:
    ...
```

```python
class LoopCaps:
    review: int = 2

    gate: int = 2

    e2e: int = 3

    ci: int = 3
```

```python
class PaceConfig:
    budget_threshold: float = 0.8

    racing_minutes: int = 30

    racing_threshold: float = 0.95

    pace_margin: float = 0.1

    weekend_weight: float = 0.5

    timezone: str = 'UTC'

    session_week_share: Mapping[str, float] = field(default_factory=dict)
```

Names above refer to the existing modules: `ModelPolicy`/`Entry`/`Track`/`DEFAULT_POLICY` in `dispatcher.models`, `LoopCaps` in `dispatcher.state`, `PaceConfig` in `dispatcher.usage`. Track stages are `spec`, `plan`, `implement`, `review`, `address-review`; model entry spelling is `provider/model[@effort]`. Existing providers are `openai` and `anthropic`.

`load_config` reads YAML; `AGENT_OPS_STATE_DIR` overrides its `state_dir`. `targets.example.yaml` is the existing YAML schema example. Loader requires a nonempty target `gate_cmd`; target `max_active`, if set, is between 1 and configured capacity. `background_wait_seconds` is a positive integer, not boolean. Direct dataclass constructors are existing supported inputs and do not contact board/provider services.
