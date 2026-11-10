# Console banner for unreadable usage (auth-dark) and over-pace usage — Design

## Decisions

- **The marker is read on `Sources`, not shared with `web/box_status.py`'s `_json`** — `_json`
  collapses "missing" and "malformed" into `None`, and this read must tell them apart (missing →
  `None`, malformed → mtime); a few duplicated lines beat widening a CLI helper's contract.
- **Missing vs. malformed is decided by the read, not by a prior existence check** — a
  `FileNotFoundError` on the read means "no marker" (`None`), so a delete racing the read never
  yields a stale mtime; any other `OSError`/`ValueError`, or JSON without a string `since`, falls
  back to the file's mtime. Cost: one more `stat` on the malformed path only.
- **`since` is passed through as the dispatcher wrote it, unvalidated** — a string that is not a
  date renders as `since Invalid Date` until the dispatcher's next pass repairs the marker
  (ADR 0013: a corrupt `since` resets the incident); validating it in the web layer would
  duplicate the dispatcher's repair rule.
- **`auth_dark` is a required, nullable field in the generated types** — FastAPI's output schema
  marks defaulted fields required (as with `PriorityView.pinned`), so every typed `UsageView`
  literal in the frontend tests gains `auth_dark: null` in the same ticket that regenerates the
  types, or `make gate` fails on typecheck.
- **The banner lives inside the loaded Board column** — while the board itself is loading or
  errored the page returns early and no banner shows; the usage query is independent, but the
  issue places the banner above `<BoardHeader />` and the board does not depend on usage being
  readable, so it loads during an auth-dark outage.
- **`since` is shown with `new Date(since).toLocaleString()`** — `lib/format.ts` has only
  `relativeTime` ("3h ago"), which does not read as "since <time>"; no new helper.
- **One component renders both banners** — the priority rule (auth-dark beats over-pace) is one
  `if/else` over the same `useUsage()` data; splitting it into two components would need a shared
  rule to keep them exclusive.
- **Live refresh rides the existing `usage` SSE digest** — the marker joins the `usage` digest's
  path list; every dispatcher rewrite changes its size (`alerted` flips, `last_alert` appears),
  so a same-size rewrite within one mtime tick, the only way to miss a change, does not occur.

## Data model

No database. One read-only file and one API field.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `<state_dir>/auth-dark` (file, dispatcher-owned) | `since` | read only; never written by `web/` | the dispatcher is the only writer (ADR 0013); the console must not repair or delete it |
| `UsageView` (API) | `auth_dark: AuthDarkView \| null` | nullable, required in the output schema | `null` ⇔ no marker file; set ⇔ marker exists, readable or not |
| `AuthDarkView` (API) | `since: str` | non-null string | the marker's `since`, or the file's mtime (ISO UTC) when the marker is malformed |

Red-team (top 5, all absorbed above or accepted):

1. Delete racing the read → stale mtime shown as an active incident. Absorbed: the read decides.
2. Malformed `since` string → "Invalid Date". Accepted: transient, the dispatcher repairs it.
3. Dispatcher stopped with the marker on disk → banner lingers while panels show old readings.
   Accepted: the marker is the dispatcher's truth by the operator's decision; `agent-ops status`
   reports the dead dispatcher.
4. Same-size rewrite in one mtime tick → no SSE refresh. Accepted: the dispatcher's rewrites
   always change size.
5. Marker present while one provider reads (between the dispatcher's read and its delete) → both
   the banner and live readings for up to one pass. Accepted: the next pass deletes the marker.

## Identities

None. No key is widened.

## Seams

- `Sources.auth_dark_since()` in `web/sources.py` — new; the marker read (ticket 01).
- `Sources.state_fingerprint()` in `web/sources.py` — existing; its `usage` digest (ticket 01).
- `read_model.usage_view(..., auth_dark_since=...)` in `web/read_model.py` — existing, gains a
  keyword (ticket 01).
- `GET /api/usage` and `GET /api/board` via the test app — existing (ticket 01).
- `<UsageBanner />` rendered with the usage query served by msw for `/api/usage` — new
  (ticket 02); `<BoardPage />` for placement — existing (ticket 02).
