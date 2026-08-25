# Operator message queue + slot integrity — design

Date: 2026-08-12
Status: approved (brainstorm 2026-08-12)

## Problem

Two related failures were diagnosed live on the box on 2026-08-12:

1. **Silent message loss.** "Send reply & wake" stores the operator's text in
   the single `pending_reply` field and flips the task to `unpark-requested`.
   While the task waits there for capacity, any further `reply` intent is
   dropped by `_apply_one_intent` (park not in `PARK_HUMAN`/`PARK_REVIEW`)
   with only a journald warning. The web console never shows `pending_reply`
   or queued intents, so the operator cannot tell whether a message was sent,
   is waiting, or was lost. (Sibling of backlog issue #92.)

2. **Slot leak → resume deadlock.** Slots are a resource namespace
   (`MAX_SLOTS = 3`): a port pair (`8100+slot`, `5200+slot`) and the `{slot}`
   substitution in `verify_cmd`. Concurrency is governed by `capacity`, but
   `allocate_slot` counts every in-flight task holding a slot — including
   parked ones. Only `_park_for_review` frees the slot; `_park_for_input` and
   `_park_for_ci` keep it. Two human-parked tasks plus one running session
   held all three slots while the capacity gauge honestly showed 1/2, and
   `_resume_woken` silently `continue`d on task 198 every pass: a resume
   deadlock with free capacity and no operator-visible signal.

## Contract (agreed)

- **Never drop a message.** Every card — any stage, including unclaimed
  backlog issues and done/failed tasks — accepts operator messages into a
  durable per-issue queue.
- **Delivery state is visible up front and after.** The compose box states
  when the message will be delivered; sent messages are listed with their
  delivery state.
- **Delivery happens at session boundaries** (spawn and resume). Running
  sessions queue too; mail waits for the next boundary.

## Design

### 1. Message queue — data model

Per-issue append-only file: `<state_dir>/messages/<issue>.jsonl`. One JSON
record per message: `id` (uuid), `text`, `actor`, `created_at`,
`delivered_at` (null until delivered). Keyed by issue number, not task state, because unclaimed issues
have no `task-N.json` and the queue must survive done/failed/retry cycles.

New module `dispatcher/messages.py` owns the file format:
`append(state_dir, issue, text, actor)`, `undelivered(state_dir, issue)`,
`mark_delivered(state_dir, issue, ids)`, `all_messages(state_dir, issue)`.
Malformed lines are skipped with a journal warning, mirroring
`list_intents` — a corrupt line never blocks a pass.

The web keeps writing `reply` intents exactly as today: the dispatcher stays
the single writer of state. `_apply_one_intent`'s reply handler appends to
the queue **unconditionally** — the park-state check and its silent drop path
are deleted. If the task is parked (`PARK_HUMAN`/`PARK_REVIEW`), the reply
additionally requests a wake, as today.

The `pending_reply` field on `TaskState` is retired. Internal wake reasons
(CI conclusions, "operator resumed this task") become queue messages with
actor `dispatcher`, eliminating the overwrite hazard where a second message
clobbers the first.

### 2. Delivery

Messages drain at every session boundary:

- **Spawn** (`_spawn_stage`): undelivered messages are appended to the stage
  prompt as an "Operator messages" block, oldest first. This is what delivers
  pre-briefings left on unclaimed backlog cards at claim time.
- **Resume** (`_resume_woken`, `_retry_plan`): same block appended to the
  resume prompt.

After a successful spawn/resume the drained messages are stamped
`delivered_at`. Done/failed cards accept and hold messages; they deliver only
if the task is retried/restarted.

### 3. Web console visibility

- **Task page**: a message thread above the compose box. Each operator
  message shows a state chip: *sending* (intent file written, not yet
  drained), *queued* (in the messages file, `delivered_at` null),
  *delivered* (timestamp). The read model derives *sending* from the intents
  dir and the rest from the messages file.
- **Compose box**: states the delivery contract before send, derived from
  task state — "will deliver when the session resumes — waiting for a free
  slot", "will deliver when this task is claimed", "will deliver if this
  task restarts".
- **Board card**: an `✉ n` badge when undelivered messages exist.

### 4. Slot integrity

- Every park that ends the session frees the slot (`slot = NO_SLOT`):
  `_park_for_input` and `_park_for_ci` join `_park_for_review`. Only
  `PARK_LOGIN` keeps its slot — its pane stays live on purpose. The resume
  path already reallocates from `NO_SLOT`.
- `MAX_SLOTS` is derived, not hard-coded: `capacity + 2` headroom so a
  resume can grab a fresh slot while another session is mid-teardown, and
  raising `capacity` in `targets.yaml` can never silently reintroduce
  starvation.
- **Reconcile sweep** at the top of each pass: any non-login parked task
  still holding a slot gets it freed. Makes the fix retroactive for state
  written by older code — no manual state surgery.
- **Starvation is loud**: when `_resume_woken` (or feedback/claim spawning)
  skips a wake for want of capacity or a slot, an edge-triggered
  `wake-blocked` event is appended to the eventlog, and the board card shows
  "waiting for a free slot" instead of nothing.

### 5. Slot colors

Fixed palette indexed by slot number — slot 0 renders the same hue
everywhere. Cards holding a slot get a colored border/chip; the capacity
gauge renders matching segments. Post-fix only live sessions (and login
parks) hold slots, so the colors truthfully mark running sessions. Palette
follows the dataviz-safe treatment (readable in light and dark themes).

### 6. Error handling & testing

- Queue file corruption: skip bad lines, warn, continue.
- Unit tests: queue append/drain/stamp round-trip; reply handler never
  drops regardless of park state; `pending_reply` migration; park frees slot
  for both leaking parks; reconcile sweep; derived `MAX_SLOTS`;
  `wake-blocked` event emission (edge-triggered, no per-pass spam).
- Read-model tests: message states (sending/queued/delivered), compose-box
  contract strings, card badge counts.
- E2E: reply to a parked task while all slots are held → message visible as
  queued with the correct contract string → a slot frees → task resumes and
  the message is delivered and stamped.

## Out of scope

- Injecting messages into live panes mid-stage (attach is the real-time
  channel).
- Multi-target message routing changes; the queue is per-issue within the
  existing single-target flow.
