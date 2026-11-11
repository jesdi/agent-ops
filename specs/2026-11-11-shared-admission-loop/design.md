# One box-wide admission loop for woken and feedback tasks — Design

## Decisions

- **A lazy generator, not a precomputed admitted list** — the capacity and
  slot checks must see the previous candidate's spawn, so the generator only
  advances after the caller's action ran; the caller must not collect it
  into a list first.
- **The generator ends at the token; the spawn stays with the caller** —
  crash handling differs (`_resume_woken` fails one task, `_spawn_feedback`
  lets it propagate) and the spec keeps that; a caller `try` around its own
  action keeps both, at the cost of two short loops instead of one.
- **`choose` returns `(launch, why)`** — `_wake_launch` already has that
  shape; `_spawn_feedback` drops `bypass_usage` from `_choose_launch`, which
  it never used.
- **One slot rule for both queues: keep a recorded slot only if no other
  slot holder records it, else `allocate_slot`** — fixes the in-pass
  collision a woken task with a recorded slot can hit today (a `PARK_LOGIN`
  task woken this pass records a slot it no longer holds, so an earlier
  spawn in the same pass can be allocated that number), and keeps the
  number a resumed conversation already knows (ports `8100+slot`). Costs one
  behaviour change: a feedback task recording a free slot keeps it instead
  of taking the lowest free one (open question on the review page).
- **The generator yields the task with its slot, unsaved** — the spawn
  action saves it as it does today; a skipped task never gets a slot
  written.
- **Name `_admissions`** — `_admitted` already exists (`main.py`, the admit
  wrapper `_choose_launch` uses).
- **Wakes before feedback stays** — `run_pass` keeps calling
  `_resume_woken` then `_spawn_feedback`; one generator per call.

Red-team (no new data; the shared state is the task files and slot
numbers):
1. Same-pass slot collision for a woken task with a recorded slot — closed
   by the slot rule.
2. A caller that materialises the generator (`list(...)`) defeats the
   per-candidate re-read — closed by the "spawn fills the box" scenario.
3. A candidate whose file changed after the list was read (parked for
   access by `_task_token`, failed by a crash) — same snapshot semantics as
   today; the generator re-reads only for capacity and slots.
4. An exception inside the generator (`_park_stranded`, `_task_token`)
   leaves the pass in both callers — unchanged from today, where those calls
   sit outside the `try` too.
5. A recorded slot at or above `max_slots` after capacity was lowered is
   kept when free — unchanged for wakes; ports stay distinct, so harmless.

## Data model

None. No field, file or marker is added; `slot`, `park`,
`feedback_pending`, `feedback_cursor` and the wake-blocked marker keep their
meaning.

| Table | Field | Constraint | Why |
|---|---|---|---|
| — | — | — | — |

## Identities

None.

## Seams

- `dispatcher/main.py::_resume_woken(cfg, deps, admit, order, dry_run)` —
  existing; the woken-queue scenarios drive through it.
- `dispatcher/main.py::_spawn_feedback(cfg, deps, admit, order)` —
  existing; the feedback-queue scenarios drive through it.
- `dispatcher/main.py::run_pass` — existing; the box-wide "every refused
  task is marked" scenario (`tests/test_wake_blocked_box_wide.py`).
- `dispatcher/main.py::_admissions(cfg, deps, wants, choose)` — new,
  internal; not a test seam, reached through the two above.
