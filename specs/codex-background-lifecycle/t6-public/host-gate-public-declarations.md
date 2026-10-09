# Existing task and stage gate — public declarations

Approved public supplement to the T6 runtime delivery contract.

Existing public `dispatcher.state` declarations:

```python
TaskState(issue: int, target: str, stage: Stage, slot: int, worktree: str,
          branch: str, title: str, updated_at: str,
          park: str = "", ticket_cursor: int = 0, ticket_count: int = 0, ...)
StageSignal(stage: str, status: str, note: str = "", artifact: str = "",
            run_id: int = 0, loop: str = "", round: int = 0, track: str = "")
save(state_dir: str | Path, ts: TaskState) -> None
load(state_dir: str | Path, target: str, issue: int) -> TaskState | None
read_stage_signal(worktree: str | Path) -> StageSignal | None
```

`TaskState.continued_stage` is SPEC for AWAITING_SPEC_REVIEW and otherwise the stored stage. Existing prompt-running stages are SPEC (`spec`), PLAN (`plan`), IMPLEMENT (`implement`), REVIEW (`review`), ADDRESS_REVIEW (`address-review`). Other existing stage values include queued, awaiting-spec-review, pr-open, blocked, failed, stalled-on-budget, done, canceled. A continued-stage alias does not make a nonworking approval stage eligible for background result delivery.

A working signal is `.agent/stage.json` in the bound worktree with `stage` equal to the launch stage and `status:"working"`; other native existing fields keep their meanings. StageSignal has no ticket field. IMPLEMENT ticket authority is the current TaskState.ticket_cursor, compared as its ticket string with the launch binding. Do not repurpose StageSignal.run_id (CI run identity), loop/round, or another field as a ticket selector. A changed TaskState ticket disables that launch even if the same-stage signal remains working.

The proposed T6 gate freshly reads TaskState and StageSignal at each proposal and physical send admission and requires the same target/issue/worktree, actual working stage/continued stage and IMPLEMENT ticket, empty park, live nonretired snapshot service, and matching working signal. Missing/malformed/nonworking/mismatched evidence holds only result proposal/send. Bootstrap/operator admission and existing dispatcher closure/loop/capacity/approval rules remain unchanged. A valid working signal does not override task state, a changed ticket or a retirement fence. REVIEW done to PR_OPEN blocks old-stage sends even when its physical terminal remains alive.

Fixture setup may use the existing public save/load boundary and write its OWN stage artifact. It must not inspect or import old fixture bodies or mutate real task state. No new TaskState/StageSignal field, gate mutation or snapshot collection is declared.

## Retained task storage public fixture declarations

Approved public setup declaration; no implementation body is supplied.

```python
# dispatcher.state
def task_key(target: str, issue: int) -> str: ...
def save(state_dir: str | Path, ts: TaskState) -> None: ...
def load(state_dir: str | Path, target: str, issue: int) -> TaskState | None: ...
```

The retained task identity key is `<target>-<issue>`; current saved-task files are:

```text
<state_dir>/task-<target>-<issue>.json
```

Targets may contain hyphens. Task identity comes from the saved JSON record, with the exact owning target and issue, rather than interpreting another task's filename as authority. Legacy `<state_dir>/task-<issue>.json` fallback may supply only its own matching target; isolated fixtures need no legacy file.

TaskState is a frozen dataclass. Change an OWN fixture TaskState using `dataclasses.replace` then the public `save`. To exercise missing/malformed task evidence, the fixture may unlink or corrupt ONLY its OWN selected current task file after creating it through public save. Preserve/restore that file for cleanup if required, and do not mutate runtime snapshots, host credentials, another task's file, or live task state. Use public load/save for ordinary valid task changes.

`load` returns None when the addressed task is absent (and no owning legacy record exists). Malformed directly addressed JSON/record may raise JSONDecodeError, KeyError, TypeError or ValueError. T6's approved fresh task gate must hold proposal/send on missing/malformed evidence; that promise belongs to the runtime gate and does not redefine the existing public task reader's errors. No private `_path`/`_read` import or mock is required.
