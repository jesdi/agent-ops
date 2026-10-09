# Existing task-owned StageSignal artifact

This supplement declares existing interfaces. T8 implementation and genuine native
actual-launch verification remain required; settled behavior is in the
[presentation contract](../../runtime-presentation-contract.md).


```python
class StageSignal:
    stage: str

    status: str

    note: str = ''

    artifact: str = ''

    run_id: int = 0

    loop: str = ''

    round: int = 0

    track: str = ''

def read_stage_signal(worktree: str | Path) -> StageSignal | None:
    ...
```

Path: `<worktree>/.agent/stage.json`. Encoding: one UTF-8 JSON object. Required string members are `stage` and `status`; optional members and constructor defaults are declared above. Working stage spellings are `spec`, `plan`, `implement`, `review`, `address-review`; a minimal working IMPLEMENT signal is:

```json
{"stage":"implement","status":"working"}
```

This is a task-owned worktree artifact; owned setup may create its parent and write those bytes. It is not a runtime snapshot, host claim, receipt or input-admission record. Invalid/unreadable/absent signals return no StageSignal. The supported status vocabulary includes `working`, `awaiting-review`, `done`, `blocked`, `awaiting-ci`; stage policy decides applicability. The artifact does not carry a ticket identity. Saved TaskState supplies the current ticket/stage association.

Presentation eligibility has no working StageSignal prerequisite. Result proposal/send retains its separate existing StageSignal gate. Saved Stage enum strings are declared in the state record supplement; `awaiting-spec-review` is a saved stage whose continued runtime stage is `spec`, not a new working signal spelling.
