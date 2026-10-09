# Existing production composition and isolated external boundaries

This supplement declares existing interfaces. T8 implementation and genuine native
actual-launch verification remain required; settled behavior is in the
[presentation contract](../../runtime-presentation-contract.md).


```python
class Deps:
    github: object

    sessions: object

    notifier: object

def run_pass(cfg: Config, deps: Deps, dry_run: bool=False, config_path: str='targets.yaml') -> None:
    ...

def guarded_pass(cfg: Config, deps: Deps, config_path: str, dry_run: bool=False) -> None:
    ...
```

`run_pass` is the existing public production entrypoint. Existing internal `_run_pass` has the same parameters/return type and is a Touches location only, not a new public/test caller seam. The separately declared T8 presentation callable is the direct isolated seam. H41 requires production to reuse that callable; the declaration does not promise an independently injected whole pass.

`Deps` has exactly the three existing fields shown above; it does not accept usage, inbound, artifact, clock or presentation callbacks. Its GitHub/session/notifier objects implement the following genuine external interfaces. The OWN recording notifier uses the existing `send` signature, while real task audit calls remain `dispatcher.eventlog`; pane captures are not task audit history.

```python
class Candidate:
    number: int

    title: str

    url: str

    effort: int | None = None

    labels: tuple[str, ...] = ()

class GitHubClient:
    def __init__(self, dry_run: bool=False):
        ...

    def viewer_login(self) -> str:
        ...

    def pr_view(self, target: Target, pr_number: int) -> dict:
        ...

    def ci_statuses(self, target: Target, head: str, branch: str) -> list[CIStatus]:
        ...

    def pr_number_for_branch(self, target: Target, branch: str) -> int:
        ...

    def run_status(self, target: Target, run_id: int) -> str:
        ...

    def rank_rows(self, target: Target) -> list[dict]:
        ...

    def candidates(self, target: Target) -> list[Candidate]:
        ...

    def issue_view(self, repo: str, number: int) -> dict:
        ...

    def issue_state(self, repo: str, number: int) -> str:
        ...

    def create_issue(self, repo: str, title: str, body: str) -> int:
        ...

    def set_status(self, target: Target, issue: int, option_id: str) -> None:
        ...

    def set_boost(self, target: Target, issue: int, value: int) -> None:
        ...

    def add_label(self, target: Target, issue: int, label: str) -> None:
        ...

    def comment(self, target: Target, issue: int, body: str) -> None:
        ...

    def claim(self, target: Target, cand: Candidate) -> None:
        ...

    def release(self, target: Target, issue: int, reason: str) -> None:
        ...

    def cancel(self, target: Target, issue: int) -> None:
        ...

    def delete_branch(self, target: Target, branch: str) -> None:
        ...

    def append_blocked_by(self, target: Target, issue: int, blocker: int) -> None:
        ...
```

```python
class CIStatus:
    conclusion: str

    completed_at: str
```

```python
class Notifier:
    def __init__(self, dry_run: bool=False, console_url: str='', multi_target: bool=False):
        ...

    def send(self, template: str, **ctx) -> int:
        ...
```

Session interfaces are in [Sessions/Config](03-sessions-config-public-declarations.md). Config/model/loop/pace records there are the complete inputs for retained capacity, strict-cap, stage/loop and queue behavior. Existing saved state record fields are declared below; stage enum values are `queued`, `spec`, `awaiting-spec-review`, `plan`, `implement`, `review`, `pr-open`, `address-review`, `blocked`, `failed`, `stalled-on-budget`, `done`, `canceled`.

```python
class SpecApprovalRequest:
    kind: str = 'spec-approval'

class AnswersRequest:
    path: str

    kind: str = 'answers'

class TaskState:
    issue: int

    target: str

    stage: Stage

    slot: int

    worktree: str

    branch: str

    title: str

    updated_at: str

    park: str = ''

    ci_run_id: int = 0

    park_msg_id: int = 0

    park_note: str = ''

    hold_for_attach: bool = False

    effort: int | None = None

    labels: tuple[str, ...] = ()

    track: str = ''

    picks: dict[str, str] = field(default_factory=dict)

    spec_retries: int = 0

    plan_retries: int = 0

    pr_number: int = 0

    feedback_cursor: str = ''

    feedback_pending: bool = False

    terminal_at: str = ''

    done_at: str = ''

    spec_path: str = ''

    ticket_cursor: int = 0

    ticket_count: int = 0

    review_rounds: int = 0

    gate_rounds: int = 0

    e2e_rounds: int = 0

    ci_rounds: int = 0

    check_cursor: str = ''

    conflict_cursor: str = ''

    attention: str = ''

    resume_model_override: str = ''

    resume_bypass_usage: bool = False

    crashed_stage: str = ''

    background_reported: float = 0.0

    background_seq: int = 0

    operator_request: 'OperatorRequest | None' = None

    @property
    def continued_stage(self) -> Stage:
        ...

def save(state_dir: str | Path, ts: TaskState) -> None:
    ...

def load(state_dir: str | Path, target: str, issue: int) -> TaskState | None:
    ...

def load_all(state_dir: str | Path) -> list[TaskState]:
    ...
```

`TaskState`, `Candidate`, `CIStatus`, the operator request records, usage records and registration records above/below are dataclasses; TaskState and the mentioned immutable value records are frozen. `OperatorRequest` is the existing alias `SpecApprovalRequest | AnswersRequest`. `Deps` is a mutable dataclass; its constructor is `Deps(github:object, sessions:object, notifier:object)`.

External boundaries outside Deps:

- `telegram.inbound.fetch_events(state_dir: str | Path) -> list` uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; when either is unset it yields an empty list without Telegram polling. With credentials, its cursor artifact is `<state_dir>/telegram-offset` and its external service is Telegram getUpdates. This is not an inbound callback parameter to run_pass.
- Usage is fetched through the existing provider cache and genuine adapters below. `run_pass` supplies Config, not an adapter argument. Adapter injection supported by standalone `fetch_all` is not promised as pass injection.
- Triage has existing request/cursor artifacts and genuine herdr process boundary. Its public operations below retain production behavior; owning an isolated herdr server is an external prerequisite, not a replacement `running()` implementation.
- Artifact collection/publication has the existing worktree registration artifact and git `origin` boundary below. It is not supplied through Deps. Standard owned git worktrees/origin and declared artifact inputs are the existing artifact route; no replacement collector/publisher callback is added.

Usage records and cache artifact:

```python
class Window:
    kind: WindowKind

    scope: str | None

    used: float

    resets_at: datetime

    @property
    def length(self) -> timedelta:
        ...

class ProviderUsage:
    provider: str

    source: Source

    fetched_at: float

    windows: tuple[Window, ...]

def unavailable(provider: str, fetched_at: float) -> ProviderUsage:
    ...
```

```python
class UsageAdapter(Protocol):
    name: str

    def fetch(self, state_dir: Path, *, now: Callable[[], float]=time.time) -> ProviderUsage:
        ...

def usage_to_json(u: ProviderUsage) -> dict:
    ...

def usage_from_json(d: dict) -> ProviderUsage:
    ...

def cache_path(state_dir: str | Path, provider: str) -> Path:
    ...

def fetch_provider(name: str, state_dir: str | Path, *, now: Callable[[], float]=time.time, adapters: dict[str, UsageAdapter]=ADAPTERS) -> ProviderUsage:
    ...

def fetch_all(cfg: Config, *, now: Callable[[], float]=time.time, adapters: dict[str, UsageAdapter]=ADAPTERS) -> dict[str, ProviderUsage]:
    ...
```

`WindowKind` serialized values are `session` and `weekly`; source values are `oauth`, `ccusage`, `unavailable`. Path: `<state_dir>/usage/<provider>.json`. Existing cache layout:

```json
{"fetched_at":0.0,"usage":{"provider":"openai","source":"oauth","fetched_at":0.0,"windows":[{"kind":"session","scope":null,"used":0.1,"resets_at":"2030-01-01T00:00:00+00:00"}]}}
```

The outer fetch time must be current: a cache hit requires age at least zero and strictly less than 180 seconds. A usable reading has nonempty windows or source `unavailable`; unreadable/invalid/stale caches are misses and may consult adapters. `resets_at` is an aware ISO datetime, `used` is a fraction, `scope` is string or null. This owned cache is provider-usage fixture data, not runtime evidence/history or an admission override. It retains the real allowance calculations. Native model-service traffic and provider usage/account traffic are distinct external interfaces.

Triage artifacts: `<state_dir>/triage-request.json` is an object with `requested_at:string` (ISO timestamp); `<state_dir>/triage_cursors.json` is a repository-to-cursor string mapping. The existing owned herdr tab label is `triage`, workspace `agent-ops`.

Triage interfaces:

```python
def running() -> bool:
    ...

def load_request(state_dir: str | Path) -> str | None:
    ...

def enqueue(state_dir: str | Path) -> bool:
    ...

def clear_request(state_dir: str | Path) -> None:
    ...

def pending(state_dir: str | Path) -> bool:
    ...

def load_cursors(state_dir: str | Path) -> dict[str, str]:
    ...

def save_cursors(state_dir: str | Path, cursors: dict[str, str]) -> None:
    ...

def tick(cfg: Config, deps, config_path: str) -> None:
    ...
```

Artifact registration: `<worktree>/.agent/artifacts.json` is a JSON array of at most 100 unique-ID objects `{"id":string,"name":string,"path":string}`. ID matches `[a-z0-9][a-z0-9_-]{0,79}`; name is nonblank and at most 200 characters; sources are contained in the owned worktree. Existing automatic artifacts include `.agent/prototype.html`, `.agent/questionnaire.md`, current answers request and `TaskState.spec_path`. The publication external dependency is git in the worktree with its configured remote `origin`; publication operations are not intercepted by `Deps.github`.

```python
class Registration:
    id: str

    name: str

    path: str

    @classmethod
    def parse(cls, raw: dict) -> Registration:
        ...

def read(state_dir, target: str, issue: int) -> ArtifactIndex:
    ...

def collect(state_dir, task: TaskState, repo: str, *, publish: bool=True, now: datetime | None=None) -> None:
    ...

def pin_published(state_dir, task: TaskState, repo: str) -> None:
    ...

def cleanup(state_dir, *, now: datetime | None=None) -> None:
    ...

def content_path(state_dir, target: str, issue: int, artifact_id: str) -> Path | None:
    ...
```

```python
class PublishResult:
    url: str = ''

    error: str = ''

def spec_url(repo: str, branch: str, artifact: str) -> str:
    ...

def relative_artifact(worktree: str, artifact: str) -> str | None:
    ...

def ensure_published(*, worktree: str, branch: str, repo: str, issue: int, artifact: str, dry_run: bool=False) -> PublishResult:
    ...

class PublishedReference:
    url: str

    commit: str

class ArtifactPublisher:
    def __init__(self, worktree: str, branch: str, repo: str):
        ...

    def reference(self, artifact: str, content: bytes) -> PublishedReference | None:
        ...
```

**Composition scope:** declarations support owned Config/state/signal/queue/usage/artifact inputs and existing recording external GitHub/notifier boundaries while calling the real pass. Native Sessions/herdr/Podman must remain real for native criteria. There is no declaration-only guarantee that every retained H42/N17 scenario avoids all additional external operations: scenarios involving publication, git/gh, triage or native setup require their own owned external resources/configuration and the recorded verified merged-T7 source binding. If the acceptance driver needs an undeclared executable/protocol interface, report that exact gap rather than monkeypatching production functions or inventing another pass seam. No T8 detail-key spelling or behavior policy is selected here.

## Existing default background cap note

With `background_wait_seconds=10800`, the existing cap park note is exactly:

```text
(background work still running after 180m — cap reached)
```

Equality at 10800 seconds holds; automatic cap parking requires strictly greater elapsed time and confirmed conditional retirement. This is an existing operator literal, not a new policy. Native fixture evidence must retain the real saved `park_note` and existing notification context.

## Public dependency returns and retained review grace

`GitHubClient.run_status(target, run_id)` returns the completed run's conclusion unchanged,
or the empty string when the run is not completed or its conclusion is absent.
Examples of completed conclusions are `success`, `failure`, `cancelled`, `skipped`,
and `timed_out`. Raw GitHub CLI `in_progress` is not this interface's pending value.
Owned recording GitHub boundaries use the same return vocabulary.

The saved default-cap `park_note` is exactly the literal above. The notification
`note` starts with that literal and includes the existing `\n\n` plus captured
terminal tail when that tail is nonempty; whole-note equality is not required.

Disabled or unexpired spec-review grace retains the live session and operator
artifact. It does not imply immediate session closure. Owned external Sessions.end
recorders must perform the real public conditional RuntimeClient retirement fence
before recording the ended launch, preserving dispatcher retirement authority.
