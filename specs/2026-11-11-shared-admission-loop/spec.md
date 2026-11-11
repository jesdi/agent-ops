# One box-wide admission loop for woken and feedback tasks — Spec

## Requirements

1. A single generator in `dispatcher/main.py` (working name `_admissions`; `_admitted` is
   already taken by the admit wrapper of `_choose_launch`)
   takes `cfg`, `deps`, a predicate `wants(task) -> bool` and a launch chooser
   `choose(repository, task) -> (launch | None, why: str)`, and yields, one at
   a time, `(repository, task, launch, gh_config)` for every task it admits.
   Both `_resume_woken` and `_spawn_feedback` are written as a `for` loop over
   it; neither function keeps its own sort, `_box_free` call,
   `allocate_slot` call, `_mark_wake_blocked` call, `_park_stranded` call or
   `_task_token` call.
2. **Selection and order.** The generator considers only tasks whose
   repository is in `cfg.repositories` and for which `wants(task)` is true,
   in `_oldest_first(cfg)` order: oldest `updated_at` first, ties in
   repository-list order. The candidate list is read once, when iteration
   starts.
3. **Launch choice.** For each candidate it calls `choose`. A non-empty
   `why` parks the task with `_park_stranded(cfg, deps, repository, task,
   why)`. A `None` launch skips the task (nothing else happens to it this
   pass, no wake-blocked mark).
4. **Capacity.** With a launch, it re-reads `load_all(cfg.state_dir)` and
   when `_box_free` is `<= 0` marks the task `_mark_wake_blocked(...,
   "capacity full")` and skips it. The re-read happens per candidate, after
   the caller's spawn action for the previous candidate has run (the
   generator is lazy, not a precomputed list), so a spawn that fills the box
   refuses every later candidate in the same pass.
5. **Slot.** The task keeps its recorded slot when it has one
   (`slot != NO_SLOT`) and no other task that holds a slot (`holds_slot`)
   records the same number; otherwise it gets `allocate_slot(load_all(...),
   max_slots(cfg.capacity))`. When that returns `None` the task is marked
   `_mark_wake_blocked(..., "no free slot")` and skipped. The yielded task
   carries the slot (`replace(task, slot=...)`); the generator does not save
   it.
6. **Token.** It then calls `_task_token(cfg, deps, repository, task)`; a
   `None` result skips the task with no wake-blocked mark (it stays woken or
   pending, or was parked for access by `_task_token`).
7. **Every refused task is marked.** A capacity or slot refusal never stops
   the iteration: every later candidate is still considered and marked when
   refused (the #135 behaviour, `tests/test_wake_blocked_box_wide.py`).
8. **`_resume_woken`** supplies `wants = task.park == PARK_WAKE`,
   `choose = _wake_launch(cfg, deps, repository, task, admit, order)`, and
   as its action runs `_resume_one` then `_consume_execution_choice`, with a
   crash failing that task alone via `_fail_task_crash(..., dry_run)` and the
   iteration carrying on.
9. **`_spawn_feedback`** supplies `wants = _awaiting_feedback_round`,
   `choose` = `_choose_launch(cfg, repository, task, Stage.ADDRESS_REVIEW,
   admit, order, _execution_override_for(cfg, task, Stage.ADDRESS_REVIEW))`
   reduced to `(launch, why)`, and as its action runs, in today's order:
   `_clear_wake_blocked`, set `feedback_pending=False` and
   `feedback_cursor=_cursor_now()`, `_end_session`, `_spawn_stage`,
   `_consume_execution_choice`. An exception from the action still leaves
   the pass as today.
10. Behaviour is otherwise unchanged: every existing test in `tests/` passes
    unmodified, apart from tests that assert the old slot behaviour that
    requirement 5 replaces (none is known; any such change is named in the
    PR).
11. `make gate` passes.

## Scenarios

### Scenario: oldest wake is resumed first across repositories

- **Given** a box with capacity 1 and no task holding a slot, repositories
  `portfolio_eval` then `factorial`, task `factorial#42` with `park=wake`,
  `updated_at=2026-07-21T10:00:00Z`, and task `portfolio_eval#3` with
  `park=wake`, `updated_at=2026-07-21T10:05:00Z`
- **When** `_resume_woken` runs with every launch admitted
- **Then** `factorial#42` is resumed in slot 0, and `portfolio_eval#3` is not
  resumed and gets one `wake-blocked` event with detail `capacity full`

### Scenario: equal `updated_at` breaks ties in repository-list order

- **Given** capacity 1, repositories `portfolio_eval` then `factorial`, and
  two `pending-feedback` pr-open tasks `factorial#5` and `portfolio_eval#7`
  with the same `updated_at`
- **When** `_spawn_feedback` runs with every launch admitted
- **Then** `portfolio_eval#7` is spawned into address-review and
  `factorial#5` is marked `capacity full`

### Scenario: every refused task is marked, not just the first

- **Given** a full box (3 tasks holding slots 0–2, capacity 3), woken tasks
  `portfolio_eval#3` and `factorial#42`, and pr-open feedback tasks in both
  repositories
- **When** `run_pass` runs
- **Then** each of the four refused tasks has exactly one `wake-blocked`
  event with detail `capacity full` (the existing
  `tests/test_wake_blocked_box_wide.py` cases stay green)

### Scenario: a spawn that fills the box refuses the next candidate in the same pass

- **Given** capacity 2, one task holding slot 0, and two woken tasks with no
  slot
- **When** `_resume_woken` runs
- **Then** the older one is resumed in slot 1 and the newer one is marked
  `capacity full` (the generator re-reads the state after the first spawn)

### Scenario: two slot-less wakes get distinct slots

- **Given** capacity 3, no task holding a slot, two woken tasks with
  `slot=NO_SLOT`
- **When** `_resume_woken` runs
- **Then** they are resumed in slots 0 and 1 (the existing
  `test_two_slot_less_woken_tasks_get_distinct_slots` stays green)

### Scenario: boundary — no free slot while the box still has capacity

- **Given** every slot number below `max_slots(capacity)` held while
  `_box_free > 0`, and one woken task with `slot=NO_SLOT` (the setup of the
  existing `test_woken_gate_parked_task_waits_when_every_slot_is_taken`)
- **When** `_resume_woken` runs
- **Then** it is not resumed and gets one `wake-blocked` event with detail
  `no free slot`; a second pass adds no second event

### Scenario: a recorded slot nobody else holds is kept

- **Given** capacity 3, task `portfolio_eval#1` holding slot 0, and a woken
  task `portfolio_eval#3` recording `slot=2`
- **When** `_resume_woken` runs
- **Then** `portfolio_eval#3` resumes in slot 2; likewise a pr-open feedback
  task recording `slot=2` that nobody else holds is spawned by
  `_spawn_feedback` in slot 2 (today it would take slot 1)

### Scenario: violation — a recorded slot another task holds is replaced

- **Given** capacity 3, task `portfolio_eval#1` holding slot 0, and a
  pr-open feedback task `factorial#5` still recording `slot=0` (it reached
  pr-open this pass, before `_reconcile_slots` cleared it)
- **When** `_spawn_feedback` runs
- **Then** `factorial#5` is spawned in slot 1, never slot 0; the same holds
  for a woken task recording a slot another task holds

### Scenario: a stranded launch parks the task and spawns nothing

- **Given** a woken task whose launch chooser returns `(None, "<why>")`
  with a non-empty why
- **When** `_resume_woken` runs
- **Then** the task is parked through `_park_stranded` with that why, is not
  resumed, and gets no `wake-blocked` event; the same for a feedback task
  under `_spawn_feedback`

### Scenario: a denied launch waits without a mark

- **Given** a woken task and a feedback task, and an `admit` that denies
  every model
- **When** `_resume_woken` and `_spawn_feedback` run
- **Then** neither is spawned, neither gets a `wake-blocked` event, the
  woken task keeps `park=wake` and the feedback task keeps
  `feedback_pending=True`

### Scenario: a failed task token skips without a mark

- **Given** a woken task with a free slot and an admitted launch, and a
  `_task_token` that returns `None`
- **When** `_resume_woken` runs
- **Then** the task is not resumed, keeps `park=wake`, and gets no
  `wake-blocked` event; the same under `_spawn_feedback` for a feedback
  task, which keeps `feedback_pending=True`

### Scenario: a task woken during the iteration waits for the next pass

- **Given** capacity 3 and one woken task `portfolio_eval#3`, whose resume
  action saves another task `factorial#42` with `park=wake`
- **When** `_resume_woken` runs
- **Then** only `portfolio_eval#3` is resumed; `factorial#42` keeps
  `park=wake` and gets no `wake-blocked` event (the candidate list was read
  when iteration started)

### Scenario: a resume crash fails only that task

- **Given** capacity 3 and two woken tasks, where resuming the older one
  raises
- **When** `_resume_woken` runs
- **Then** the older task is FAILED with a `task-crash` report and the newer
  one is still resumed

### Scenario: a feedback spawn starts a fresh round

- **Given** a pr-open task with `feedback_pending=True` and a free slot
- **When** `_spawn_feedback` runs
- **Then** its session is ended before `_spawn_stage`, it is spawned into
  address-review with `feedback_pending=False` and a second-quantised
  `feedback_cursor`, its wake-blocked marker is gone, and its stored
  execution choice is consumed

### Scenario: one implementation

- **Given** the refactored `dispatcher/main.py`
- **When** it is read
- **Then** `_box_free`, `allocate_slot`, `_mark_wake_blocked`,
  `_park_stranded`, `_task_token` and `_oldest_first` are called only
  inside the shared generator, never in `_resume_woken` or
  `_spawn_feedback` (`_mark_wake_blocked` keeps one call per refusal
  reason), and `make gate` passes

## Out of scope

- One merged queue for wakes and feedback; wakes still go first.
- Crash handling in `_spawn_feedback` (still propagates).
- Where `_clear_wake_blocked` runs.
- Claiming and `onboarding_sessions.tick`.
- Renaming the two functions.
