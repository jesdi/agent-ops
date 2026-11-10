# A wrong-typed quarantine record no longer 500s /api/failures — Design

## Decisions

- **Validate in `Sources.quarantine_entries()`, not in the `failures` route** — every consumer (failures, board, retry) then sees the same filtered list; a route-level `try/except` would leave board and retry exposed and `issue_open` still called.
- **Skip a malformed record with the same `continue` as an unreadable one** — no new state; issue #87 can later surface both at once.
- **Check the body as written, before the filename overrides `task_issue`** — a corrupt body stays corrupt even when the filename supplies the key.
- **Integer fields reject booleans explicitly** — `bool` is a subclass of `int` in Python, so a bare `isinstance(v, int)` would accept `true`; floats and numeric strings are rejected, never coerced.
- **One table of key → expected type, checked only for keys present** — absent keys keep their blank defaults; extra keys are ignored as today.

## Data model

None. No record format, column or API schema changes; quarantine records stay written only by `failures.write_quarantine`.

| Table | Field | Constraint | Why |
|---|---|---|---|
| — | — | — | no stored data changes |

## Identities

None.

## Seams

- `Sources.quarantine_entries()` — existing; the unit seam for the type scenarios.
- `GET /api/failures` — existing; the route seam for the reproduction, boundary and no-lookup scenarios.
- `GET /api/board`, `POST /api/task/{repository}/{issue}/retry` — existing; the consumer scenario.
