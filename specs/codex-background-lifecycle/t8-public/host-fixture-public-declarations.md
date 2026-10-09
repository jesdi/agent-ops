# Existing host fixture interfaces for T8

Implementation and genuine native actual-launch verification remain required; these
declarations make no implementation or integration success claim. They supply the
existing owned host-state boundary for the [presentation contract](../runtime-presentation-contract.md).

`dispatcher.config.Target` is a frozen dataclass. Required constructor keywords:
`name`, `repo`, `clone_path`, `worktrees_path`, `rank_cmd`, `project_number`,
`project_owner`, `status_field_id`, `status_ready_option_id`,
`status_in_progress_option_id`. Strings except integer `project_number`; optional
`setup_cmd`, `verify_cmd`, `gate_cmd` are strings defaulting to empty. Fixture paths
must belong to its temporary workspace, repo is generic owner/name metadata, and
unused board fields can be empty metadata. Construction does not perform board calls.

`dispatcher.state.TaskState` is frozen. Required constructor keywords:
`issue:int`, `target:str`, `stage:Stage`, `slot:int`, `worktree:str`, `branch:str`,
`title:str`, `updated_at:str` (ISO UTC). Relevant optional fields include
`park:str=''`, `ticket_cursor:int=0`, `ticket_count:int=0`,
`crashed_stage:str=''`. Use `dataclasses.replace` to build a changed immutable task.
`TaskState.continued_stage` supplies the continued runtime stage. Existing
`Stage` values include SPEC, PLAN, IMPLEMENT, REVIEW, ADDRESS_REVIEW, BLOCKED,
STALLED_ON_BUDGET, FAILED, DONE, CANCELED, QUEUED, AWAITING_SPEC_REVIEW, PR_OPEN.
Use existing saved stages/park rules declared in CONTEXT and runtime contracts.
The presentation in-flight set is QUEUED, SPEC, AWAITING_SPEC_REVIEW, PLAN, IMPLEMENT,
REVIEW, ADDRESS_REVIEW, BLOCKED and STALLED_ON_BUDGET, with empty park. Continued
stage is SPEC for AWAITING_SPEC_REVIEW and otherwise the saved stage. Task/worktree/
stage/ticket/current-launch association remains required. Presentation adds no working
StageSignal prerequisite; result proposal/send retains its existing separate gate.

```python
# dispatcher.state
save(state_dir: str | Path, ts: TaskState) -> None
load(state_dir: str | Path, target: str, issue: int) -> TaskState | None

# telegram.notify
Notifier(dry_run=False, console_url='', multi_target=False)
Notifier.send(template: str, **ctx) -> int

# dispatcher.messages
append(state_dir: str | Path, target: str, issue: int,
       text: str, actor: str) -> Message
all_messages(state_dir: str | Path, target: str, issue: int) -> list[Message]
undelivered(state_dir: str | Path, target: str, issue: int) -> list[Message]
```

`Message` is frozen with `id`, `text`, `actor`, `created_at`, `delivered_at` string
fields; empty `delivered_at` denotes queued input. Owned fixture calls may seed a
queue through `append` and compare real `all_messages` before/after presentation.
Do not consume or rewrite that queue to simulate presentation.

The isolated notifier implements the external `send(template, **ctx)` interface
and records calls/return IDs; it does not call Telegram. Runtime claims and task
history must still use the real host listener and `dispatcher.eventlog` public
interfaces declared with the presentation contract. Do not replace runtime views,
claims or history with injected callbacks. All fixture state/worktrees are isolated;
no board/provider/model API, actual operator queue or deployed task is involved.
