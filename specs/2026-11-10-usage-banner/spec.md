# Console banner for unreadable usage (auth-dark) and over-pace usage — Spec

Source: issue #78, a design the operator settled on 2026-11-05. Its decisions are carried here
unchanged; "Reconciliation" records what moved in the code since the issue was written (at
`61dbbdb`) and how the design absorbs it.

Terms:

- **The marker**: the file `<state_dir>/auth-dark`, written and deleted only by the dispatcher
  (`_auth_dark_edge` in `dispatcher/main.py`). It holds JSON with at least `since` (ISO-8601 UTC,
  when the incident began); today also `alerted` and, once alerted, `last_alert`. It is written on
  the FIRST dark pass (the 30-minute grace only delays Telegram) and deleted as soon as any
  provider reads.
- **The usage view**: `read_model.UsageView`, built only in `web/app.py` `_usage_view` and served
  by `GET /api/usage`, `GET /api/board` (its `usage` field) and the usage Unix socket.
- **The banner**: the new `frontend/src/components/UsageBanner.tsx`.

## Requirements

### Backend

1. **Marker read at the I/O edge.** `Sources.auth_dark_since() -> str | None` in
   `web/sources.py`: returns `None` when the marker does not exist; otherwise the marker's `since`
   string. When the marker exists but is unreadable or malformed (not JSON, not an object, no
   string `since`), it still returns a value: the file's mtime as an ISO UTC string via the
   existing `_iso` helper. It never raises (a marker deleted between the existence check and the
   read returns `None`).
2. **Live refresh.** `Sources.state_fingerprint()`'s `usage` digest includes `root / "auth-dark"`
   next to `budget-stalled`. Creating the marker, rewriting it with a different size or mtime
   (`alerted` flips, `last_alert` is added), and deleting it each change the `usage` digest;
   after delete the digest equals the digest from before the marker existed.
3. **Read model pass-through.** `web/read_model.py` gains `class AuthDarkView(BaseModel): since:
   str`; `UsageView` gains `auth_dark: AuthDarkView | None = None`; `usage_view(...)` gains a
   keyword `auth_dark_since: str | None = None` that fills it (`None` → `auth_dark` is `None`).
   No logic beyond the pass-through.
4. **Wired into every usage view.** `web/app.py` `_usage_view` passes
   `auth_dark_since=sources.auth_dark_since()`, so `/api/usage`, `/api/board` and the usage socket
   all carry `auth_dark`.
5. **Generated types current.** `frontend/src/lib/api-types.ts` is regenerated with
   `pnpm gen:api` (from `frontend/`) and committed, never hand-edited; `UsageView.auth_dark` is
   in it. The existing usage fixtures in `frontend/src/test/fixtures.ts` (`usage`,
   `usageUnavailable`, `usageCcusage`) gain `auth_dark: null` and type-check.

### Frontend

6. **Placement.** `UsageBanner` is rendered as the FIRST child of the Board page's top-level
   column in `frontend/src/pages/BoardPage.tsx` (above `<BoardHeader />`). It calls `useUsage()`
   (the header's query: no extra request) and renders at most one banner, `role="alert"`.
7. **Auth-dark banner first.** When `usage.auth_dark` is set: tone `failed` (`banner.failed` from
   `lib/tone.ts`), copy exactly `Can't read usage: the provider login has probably expired`, and
   under it, in smaller muted text, `since <local time of auth_dark.since>` (an existing helper
   in `lib/format.ts` if one fits; otherwise `new Date(since).toLocaleString()`). Test id
   `auth-dark-banner`. The over-pace banner is not shown alongside it.
8. **Over-pace banner second.** Else, when `usage.gate.admitted === false` and
   `usage.gate.binding != null`: tone `waiting` (`banner.waiting`), copy exactly
   `Usage over pace: claims paused. The window resets <reset>`, where `<reset>` is `in ` +
   `formatDuration(usage.gate.minutes_to_reset * 60)` (e.g. `in 2h 10m`). Never "paused until":
   for a weekly window the pace allowance keeps growing, so claims usually resume before the
   reset. Test id `usage-exhausted-banner`. Derived from the live gate only; no marker, no new
   backend field.
9. **Otherwise nothing.** An admitted gate, or a closed gate with `binding: null` (an unreadable
   provider without the marker; the per-provider notice covers it), renders nothing. A loading or
   errored usage query renders nothing (the header shows its own `usage-error` notice).
10. **Disappears on its own.** No local dismiss state, timers or polling: when the usage data
    changes (the marker is deleted → the `usage` SSE digest changes → `useUsage` refetches →
    `auth_dark` is `null`), the banner re-renders from the new data and vanishes.

### Gate

11. `make gate` passes (it re-runs `pnpm gen:api` and fails on a stale `api-types.ts`).

## Scenarios

Backend scenarios are pytest against a temporary state dir; frontend scenarios are vitest in
`frontend/src/components/__tests__/UsageBanner.test.tsx` with an msw handler for `/api/usage`,
patterned on `UsagePanel.test.tsx`.

### Scenario: no marker reads as None (req 1)

- **Given** a state dir without `auth-dark`
- **When** `Sources.auth_dark_since()` is called
- **Then** it returns `None`

### Scenario: marker since is returned (req 1)

- **Given** `auth-dark` containing `{"since": "2026-11-05T10:00:00+00:00", "alerted": false}`
- **When** `Sources.auth_dark_since()` is called
- **Then** it returns `"2026-11-05T10:00:00+00:00"`

### Scenario: alerted marker with last_alert still returns since (req 1)

- **Given** `auth-dark` containing `{"since": "2026-11-05T10:00:00+00:00", "alerted": true, "last_alert": "2026-11-05T18:30:00+00:00"}`
- **When** `Sources.auth_dark_since()` is called
- **Then** it returns `"2026-11-05T10:00:00+00:00"` (not `last_alert`)

### Scenario: corrupt marker falls back to mtime (req 1)

- **Given** `auth-dark` containing `not json`
- **When** `Sources.auth_dark_since()` is called
- **Then** it returns an ISO string parseable by `datetime.fromisoformat` (the file's mtime in UTC) and raises nothing

### Scenario: JSON that is not an object falls back to mtime (req 1)

- **Given** `auth-dark` containing `[1, 2]` (or `{"alerted": true}` with no `since`)
- **When** `Sources.auth_dark_since()` is called
- **Then** it returns the file's mtime as an ISO UTC string and raises nothing

### Scenario: usage digest follows the marker's life (req 2)

- **Given** a state dir without `auth-dark`; digest D0 = `json.loads(state_fingerprint())["usage"]`
- **When** the marker is created with `{"since": "2026-11-05T10:00:00+00:00", "alerted": false}` (digest D1), then rewritten with `{"since": "2026-11-05T10:00:00+00:00", "alerted": true, "last_alert": "2026-11-05T10:30:00+00:00"}` (digest D2), then deleted (digest D3)
- **Then** D0, D1 and D2 are pairwise different and D3 equals D0

### Scenario: read model fills auth_dark (req 3)

- **Given** the usage inputs of an existing `usage_view` test
- **When** `usage_view(..., auth_dark_since="2026-11-05T10:00:00+00:00")` is called
- **Then** `view.auth_dark.since == "2026-11-05T10:00:00+00:00"`

### Scenario: read model defaults to no auth_dark (req 3)

- **Given** the same inputs
- **When** `usage_view(...)` is called without `auth_dark_since`
- **Then** `view.auth_dark is None`

### Scenario: /api/usage without and with the marker (req 4)

- **Given** the test app over a state dir without `auth-dark`
- **When** `GET /api/usage`
- **Then** the body has `"auth_dark": null`
- **And when** `{"since": "2026-11-05T10:00:00+00:00", "alerted": false}` is written to `<state_dir>/auth-dark` and `GET /api/usage` is called again
- **Then** the body has `"auth_dark": {"since": "2026-11-05T10:00:00+00:00"}`

### Scenario: /api/board carries the same field (req 4)

- **Given** the marker from the previous scenario
- **When** `GET /api/board`
- **Then** `body["usage"]["auth_dark"] == {"since": "2026-11-05T10:00:00+00:00"}`

### Scenario: generated types are current (req 5, req 11)

- **Given** the change is complete
- **When** `make gate` runs
- **Then** `pnpm gen:api` leaves `frontend/src/lib/api-types.ts` unchanged, `UsageView` in it has `auth_dark`, and `pnpm typecheck` passes with `auth_dark: null` on the three fixtures

### Scenario: auth-dark banner (req 6, req 7)

- **Given** `/api/usage` returns `usageUnavailable` with `auth_dark: { since: "2026-11-05T10:00:00+00:00" }`
- **When** `UsageBanner` renders
- **Then** an element with `role="alert"` and test id `auth-dark-banner` shows `Can't read usage: the provider login has probably expired` and a line starting with `since `, and no `usage-exhausted-banner` exists

### Scenario: auth-dark wins over a closed binding gate (req 7)

- **Given** a payload with `auth_dark: { since: "2026-11-05T10:00:00+00:00" }` AND `gate.admitted: false` with a non-null `binding` and `minutes_to_reset: 130`
- **When** `UsageBanner` renders
- **Then** only `auth-dark-banner` is shown; `usage-exhausted-banner` is absent

### Scenario: over-pace banner (req 8)

- **Given** `auth_dark: null`, `gate.admitted: false`, `gate.binding` a window view, `gate.minutes_to_reset: 130`
- **When** `UsageBanner` renders
- **Then** an element with `role="alert"` and test id `usage-exhausted-banner` shows exactly `Usage over pace: claims paused. The window resets in 2h 10m` (`formatDuration(7800)` is `2h 10m`), and the text `until` does not appear

### Scenario: closed gate without a binding window renders nothing (req 9)

- **Given** `usageUnavailable` with `auth_dark: null` (`admitted: false`, `binding: null`)
- **When** `UsageBanner` renders
- **Then** no element with `role="alert"` exists

### Scenario: admitted gate renders nothing (req 9)

- **Given** the `usage` fixture (`admitted: true`, `auth_dark: null`)
- **When** `UsageBanner` renders
- **Then** no element with `role="alert"` exists

### Scenario: errored query renders nothing (req 9)

- **Given** `/api/usage` answers 500
- **When** `UsageBanner` renders and the query settles in error
- **Then** no element with `role="alert"` exists

### Scenario: banner vanishes when the marker clears (req 10)

- **Given** `UsageBanner` shows `auth-dark-banner` from a payload with `auth_dark` set
- **When** the msw handler switches to the `usage` fixture (`auth_dark: null`, admitted) and the `usage` query is invalidated
- **Then** `auth-dark-banner` disappears and no `role="alert"` remains

### Scenario: placement above the header (req 6)

- **Given** the Board page rendered with a payload whose `auth_dark` is set
- **When** the page has loaded
- **Then** `auth-dark-banner` precedes the board header's content in document order (it is the first child of the page column)

## Reconciliation

The design was written against `61dbbdb`; checked against `main` at `46a623d`:

- **Marker format moved (#77, ADR 0013).** The marker now also holds `last_alert` once alerted.
  The design reads only `since`, which keeps its meaning (start of the incident), so it absorbs
  this unchanged; scenario "alerted marker with last_alert" pins it, and the digest scenario's
  rewrite uses the new shape.
- **`web/box_status.py` already reads the marker** (`_json(root / "auth-dark")`, from the
  2026-11-09 box-status change) for the CLI report. It is a separate read path for
  `agent-ops status`; this change adds the console's own read on `Sources` as the issue
  specifies. Whether to share the tolerant-JSON helper is a plan-stage call.
- **Next-claim forecast already says "login expired".** `read_model.local_blocker` maps a gate
  closed with reason `unavailable` to the `auth-dark` verdict, and `lib/nextClaim.ts` shows
  `login expired — log in again to resume claiming` in the header. It keys on the live gate, not
  the marker, and is a small line, not a banner; the issue's banner is additive and leaves it
  unchanged.
- **Line numbers moved; names did not.** `state_fingerprint` (usage digest still lists
  `budget-stalled` and the priority mode file), `UsageView`/`GateView`/`usage_view`,
  `_usage_view` (still the single builder), `useUsage`, `useLiveUpdates`' `usage` key,
  `banner` tones, `formatDuration` and the three fixtures all exist as the issue describes.
  `BoardPage`'s column now opens with a comment before `<BoardHeader />`; the banner goes before
  `<BoardHeader />`.

## Out of scope

- Any change to the dispatcher, the marker format, the 30-minute grace or Telegram alerts.
- Reading `budget-stalled` in the web layer (the exhausted case comes from the live gate instead).
- Changing or removing the per-provider "usage unknown" notice in `UsagePanel`.
- A dismiss button, sound, browser notification, or a banner on pages other than Board.
- A new SSE key or a new endpoint.
