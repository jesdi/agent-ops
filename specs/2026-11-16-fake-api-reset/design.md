# One reset for all fake API state — Design

## Decisions

- **One complete slice, ticket 01** — seed extraction, unified reset, caller migration and contract tests land together; this small server needs no temporary compatibility phase or standalone prefactor.
- **Shared fresh seed builders** — startup and reset share columns, capacity and next-claim builders with the existing ghost builder; fresh collections and copied parked cards prevent contamination of later seeds.
- **Restore before notifying** — restore columns, capacity, next claim and priority, clear messages/intents, then push board and usage; static provider/gate/queue/history/failure data is unchanged because no existing handler mutates it.
- **Preserve SSE clients** — live connections are transport state, not fixture data; retain subscribers through resets.
- **Sequential request contract** — await mutation responses before reset; in-flight request bodies completed later are outside the serial-test contract, requiring no new locks or cancellation protocol.
- **Fresh clock** — retain the six-minute offset; use before/after reset timestamp bounds and a measurably later reset, rather than waiting six minutes.
- **Public HTTP tests** — extend the request-fixture regression and use a bounded SSE client that closes in cleanup; existing browser flows provide rendering integration coverage.
- **True startup oracle** — run startup equivalence alone against a freshly launched server before any reset. Full-suite tests must not label an earlier reset as startup. An isolated request-only config under ignored working state can select this test alone; do not run two servers on port 8481.
- **Verified ordering** — temporary configuration disables Playwright webServer; start one external fake server per sequence and invoke exact file selections sequentially: queue-flow, park-reply-flow, pinned-tracks, board, then reverse in a new sequence. Keep the process alive across all four invocations without harness resets. Verify reporter output order and exercise both chromium and pixel-7. Temporary config files stay under .agent/.
- **Validation** — Node 24; failing HTTP acceptance tests first, minimal implementation, `make gate`, frontend build, normal and repeated full Playwright runs and ordered exercises. The box runs no browser: run HTTP-only checks locally and browser flows in browser-capable CI; missing evidence is not a pass.

## Data model

Process-local JavaScript state only; there is no database, persistence migration
or database constraint. Public HTTP acceptance tests enforce restoration.

| Table | Field | Constraint | Why |
|---|---|---|---|
| state.board | columns/cards/ghosts | Fresh original columns; parked #42 only; ghosts [73,74], boost 0 | Clear pins and movement without seed contamination |
| state.board | capacity | active 0, capacity 3, slots_used 0, max_slots 5, slots_held [] | Startup/reset agreement |
| state.board | next_claim | will-claim; widget#73; minutes_to_reset 0; blocked_by empty string; ETA now + six minutes | Clear boosted selection and stale clock |
| state.usage | priority | auto; first anthropic; options auto/anthropic/openai; pinned [] | Clear provider/pin mutations |
| state | messages/intents | Empty map/list | No delivered, queued or pending message survives |
| transport | sseClients | Retain subscribers; notify board/usage after restoration | Connected reads see restored data |

## Identities

None. No identity or key is widened.

## Seams

- **New primary seam:** POST `/__control__/reset` on the running fake server (ticket 01).
- **Existing observations:** GET `/api/board`, `/api/usage`, `/api/task/widget/42`, `/api/pending-intents`, streaming `/api/events`.
- **Existing setup:** seed-pinned, apply-intents, free-slot controls, queue boost, priority and task reply/resume requests; await each response.
- **Existing integration:** serial Playwright runner and both configured device projects.

Ticket 01 covers requirements 1–5 and all scenarios: resumed task reproduction,
mixed mutations, message isolation, startup equivalence, reset without mutations,
repeated resets, queue and clock restoration, connected consumer refresh, and
migration and suite isolation. No separate prefactor or checks-only ticket.

Four independent plan reviews passed: coverage, slice shape, blocking edges,
and prefactoring. Coverage feedback made the shared seed builders explicit in
ticket 01; no extra ticket or dependency was needed. There are no unresolved
product questions. The proposed review track is critical under the supplied
frontend rule, although no production UI behavior changes.
