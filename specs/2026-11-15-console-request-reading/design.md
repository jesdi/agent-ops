# One request reading for console board helpers — Design

## Decisions

- **Request-local context** — one immutable-in-use web reading carries effective `Config`, captured UTC time, priority mode, derived order, and usage presence; construction at each route boundary costs a small per-request object but makes source ownership explicit.
- **Usage presence is explicit** — absent usage means the local snapshot or a run branch that does not fetch it, while an empty fetched result remains a real failed reading; the extra state prevents accidental admission on a snapshot.
- **Route-specific acquisition** — the snapshot loads config and mode and derives order without usage; full board and detail also fetch usage, while run fetches it only for an unpicked, unforced task; this preserves the existing local path and avoids avoidable probes.
- **One effective configuration** — pace, capacity, `max_open`, policy, pins, routed providers and model defaults come from the request reading; a test-only changed capacity or pace exposes startup captures even though today's stored overlay changes models only.
- **Existing repository lookup** — repository identity and project membership stay in the startup maps because targets.yaml is not hot reloaded; policy selection uses the reading's effective configuration and preserves the removed-repository global fallback.
- **Route compatibility** — keep response schemas, authentication boundaries and selection precedence while migrating private helpers; this is a structural change with no persistence migration.

## Data model

| Table | Field | Constraint | Why |
|---|---|---|---|
| None (request-local value only) | effective configuration, time, mode, order, optional usage | No database constraint; value is not persisted | The repository has no database field for this change. |

The request value must distinguish usage not collected from a collected empty map. Its order is derived from its own mode, usage and time using its own configuration pace. Helpers receive it with domain inputs only and do not reload any of these sources.

## Identities

None. No persistent key or identity widens.

## Seams

- `/api/board/snapshot` — existing; local snapshot, ordering, capacity and task model/pin behavior.
- `/api/board` — existing; full board, usage/admission, candidates, capacity and claim forecast.
- `/api/task/{repository}/{issue}` — existing; task detail model, pin, policy and admission.
- `/api/usage` — existing; authenticated usage view.
- Socket app `/usage` — existing; socket permission boundary and usage view.
- `/api/task/{repository}/{issue}/run` — existing; explicit model, picked task, unpicked task and queued candidate selection.

Red-team findings shaping the seams: a settings update during one board render could split policy decisions; a true empty usage result could be treated like uncollected snapshot usage; a clock crossing a usage window could change decisions within one response; a snapshot could accidentally invoke a slow usage or ranking probe; and a shared usage helper could accidentally alter the socket route's access boundary. Each is covered by the request-local design and route acceptance criteria.
