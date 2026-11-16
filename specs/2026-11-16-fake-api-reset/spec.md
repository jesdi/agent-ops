# One reset for all fake API state — Spec

## Requirements

1. `POST /__control__/reset` returns HTTP 200 with JSON `{"ok": true}` and
   restores all mutable fake API state in one request. Restore every column's
   cards and ghosts, capacity, next-claim data, priority, messages and intents.
   Scenarios: resumed task reproduction, mixed mutations, message isolation.
2. Startup and reset use the same seed builders for columns, capacity and
   next-claim data, alongside the existing ghost builder. Capacity is
   `{active: 0, capacity: 3, slots_used: 0, max_slots: 5, slots_held: []}` in both
   paths. Reset produces fresh collections so mutations cannot contaminate
   later seeds. Priority returns to mode `auto`, first provider `anthropic`,
   options `auto`, `anthropic`, `openai`, and an empty pinned list. The only
   startup/reset difference in board and usage responses is the recomputed ETA.
   Scenarios: startup equivalence, reset without mutations, repeated resets, mixed mutations.
3. Each reset recomputes `next_pass_eta` as reset time plus six minutes, restores
   next issue 73 in repository `widget`, verdict `will-claim`, zero minutes to
   reset and empty `blocked_by`. Ghosts return in order 73 then 74, both with
   boost zero. Scenario: queue and clock restoration.
4. After restoring state, reset sends SSE `changed: ['board', 'usage']` through
   the existing push mechanism so connected consumers can refresh both domains.
   Scenario: connected consumer refresh.
5. Remove both legacy reset handlers and every legacy URL reference under
   `frontend/`; migrate all nine pre-existing call sites without moving their
   reset placement, including both resets in `board.spec.ts`. Also migrate the
   two calls in the new reproduction. Document the unified reset in the fake
   server header. Scenario: migration and suite isolation.

## Scenarios

### Scenario: resumed task reproduction (requirements 1 and 2)

- **Given** the seeded parked task `widget#42`, with slot -1 and no consumed
  capacity, and no cards in the in-progress column.
- **When** `POST /__control__/free-slot` moves #42 into in-progress and one
  `POST /__control__/reset` follows.
- **Then** reset returns 200 and `{"ok": true}`; the parked column again contains
  exactly the original #42 card, in-progress is empty, and capacity is the
  zero-slot seed specified above.

The committed reproduction is `frontend/e2e/reset-state.spec.ts`. Before the
implementation, it establishes the fixture with the old message reset and
invokes the old queue reset to expose the actual state leak rather than merely
asserting a missing new endpoint. Run from `frontend/`:
`pnpm build && pnpm exec playwright test reset-state --project=chromium --repeat-each=2`.
Both executions failed at the parked-card assertion (received `[]`) on
2026-11-16. Implementation must migrate both calls to the unified reset and
retain the assertions. No browser fixture is needed for this HTTP end-to-end
regression; it runs the actual server through Playwright's request fixture.

### Scenario: mixed mutations (requirements 1 and 2)

- **Given** a recorded fresh-server board and usage response, with startup
  capacity independently checked against the zero-slot seed.
- **When** pinned cards are seeded, #74 is boosted by 10, priority is set to
  `openai`, a reply to #42 is posted and applied, a slot is freed, another reply
  is posted but left pending, and one reset is performed.
- **Then** board and usage equal the fresh responses except for the ETA;
  pinned tasks #51 and #52 and pinned track names disappear, #42 is restored,
  and queue order, boosts, priority and capacity all match the seed.

### Scenario: message isolation (requirement 1)

- **Given** #42 has a delivered message from an earlier apply/free-slot cycle,
  an undelivered stored message from a later apply, and a pending reply intent.
- **When** reset runs once.
- **Then** `/api/task/widget/42` returns no messages and its original parked
  delivery contract; `/api/pending-intents` returns `{"intents": []}` and #42
  has zero undelivered messages. A subsequent `apply-intents` does not resurrect
  any earlier message. A new reply is the only message visible when applied.

### Scenario: startup equivalence (requirement 2)

- **Given** a newly started fake server before any reset or mutation.
- **When** board and usage are read, then reset is called and both are read again.
- **Then** startup already has `slots_used: 0`; all response fields match after
  ignoring only `next_claim.next_pass_eta`. Existing provider windows, gate,
  column metadata and other unchanged seed data remain unchanged.

### Scenario: reset without mutations (requirements 1–4)

- **Given** a canonical server with no pending intents or messages and an
  established SSE connection.
- **When** reset runs without intervening mutations, including a second
  consecutive reset.
- **Then** each call returns 200 and `{"ok": true}`, preserves canonical board
  and usage apart from recomputed ETA, leaves messages and intents empty, and
  emits `changed: ['board', 'usage']` on the same connection.

### Scenario: repeated resets (requirement 2)

- **Given** a reset server that is then mutated with boosts, pinned cards and
  task movement.
- **When** reset is called twice consecutively, then the mutation/reset cycle
  is repeated in the same process.
- **Then** every reset yields the same canonical state apart from ETA, with
  no duplicate cards, accumulated boosts or messages, or changed seed objects.

### Scenario: queue and clock restoration (requirement 3)

- **Given** boost 10 has moved #74 ahead of #73 and the old ETA was captured.
- **When** reset occurs at a later measured time.
- **Then** ghosts are ordered [73, 74] with boost 0; next claim names #73 and
  the original verdict/repository/reset/block fields. The new ETA falls between
  the timestamps immediately before and after reset plus six minutes; it is
  recomputed rather than copied from startup. Ignore only this field in full
  response equality comparisons.

### Scenario: connected consumer refresh (requirement 4)

- **Given** an established `/api/events` SSE connection and mutated board and
  priority state, with earlier mutation notifications already consumed.
- **When** reset completes.
- **Then** the connection receives an event containing exactly the changed
  domains `['board', 'usage']`; reads triggered by it see restored state.
  The existing SSE connection remains usable.

### Scenario: migration and suite isolation (requirement 5)

- **Given** the existing serial Playwright configuration (`workers: 1`,
  `fullyParallel: false`) and all reset callers migrated in place.
- **When** the full end-to-end suite runs normally and with `--repeat-each=2`,
  and the queue-flow, park-reply-flow, pinned-tracks and board files are run
  in forward and reverse order in a shared server process per run.
- **Then** tests pass without leaked cards, priority, messages or queue state.
  Searching `frontend/` for either retired reset URL returns no matches and
  the fake server header explains the unified reset. Preserve board.spec.ts's
  setup and teardown resets as well as inline reset placements elsewhere.

## Testing decisions

Use the real fake server's HTTP API as the reset contract seam: Playwright
request fixtures for state assertions and an SSE client for notification
assertions. Verify message state through task detail and pending-intent routes,
not board/usage alone. Keep existing browser flows as integration coverage;
no new UI or production seams are needed. Verify both configured device
projects in browser-capable CI. Node 24 is the supported runtime (ADR 0012).

The repository gate is `make gate`; implementation must also build the frontend
and run `pnpm exec playwright test` and its repeated variant. Playwright CLI
file arguments select files but do not guarantee execution order: the plan
must use a verified ordering mechanism or temporary runner configuration for
the forward/reverse exercise, keeping a single server alive through each
sequence. Separate fresh-server runs per file do not demonstrate isolation.
The intentional red reproduction is the SPEC-stage exception, not a claim that
the implementation gate is green. No production implementation is included.

## Out of scope

Parallel execution, per-worker servers, production backend changes, UI changes,
stale `.lavish/` notes, and changes to the existing mutation endpoints or `/api`
contract. No questionnaire or prototype is needed: the bug issue supplies the
reset contract and the explicit zero-slot seed decision. No open decisions
remain for stage 1.
