# One box-wide admission loop for woken and feedback tasks

## Why

`_resume_woken` and `_spawn_feedback` (`dispatcher/main.py`) are two copies of
the same box-wide loop: filter the tasks, sort them oldest `updated_at` first
(ties in repository-list order, `_oldest_first`), choose a launch, park a
stranded task (`_park_stranded`), refuse on a full box (`_box_free`), allocate
a slot (`allocate_slot`), mark a refused task wake-blocked
(`_mark_wake_blocked`), mint the task token (`_task_token`), then spawn. #135
had to edit both copies the same way (capacity-full marking, tie-break
order), and the next change to admission will have to do it again. The
copies have also drifted on slots: `_resume_woken` keeps a recorded slot
without checking that nobody else holds it, and `_spawn_feedback` always
takes a fresh one. A pr-open task keeps its slot number until the next pass's
`_reconcile_slots`, so a task woken in the same pass can be handed that
number by `allocate_slot` while the old value is still recorded.

## For whom

The operator, who maintains the dispatcher: one place decides who gets
admitted, so every future admission change is made once.

## Goal

One shared generator owns ordering, the stranded park, the `box_free` check,
slot allocation, wake-blocked marking and the task token. `_resume_woken` and
`_spawn_feedback` supply only their predicate, their launch choice and their
spawn action, and their observable behaviour stays what it is today except
for the one slot rule written in the spec.

## Non-goals

- Merging the two queues into one ordering: all woken tasks are still served
  before any feedback task in a pass, as `run_pass` calls them today.
- Changing crash handling: `_resume_woken` keeps failing one task on a crash
  (`_fail_task_crash`), `_spawn_feedback` keeps letting the exception leave
  the pass. Unifying that is a separate change.
- Moving `_clear_wake_blocked`: each spawn action still clears the marker
  where it does today.
- Folding `onboarding_sessions.tick` or the claim loop into the generator.
- Renaming `_resume_woken` / `_spawn_feedback`: tests and `run_pass` call
  them by name.
