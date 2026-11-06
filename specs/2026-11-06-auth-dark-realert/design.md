# auth-dark re-alerts during a long outage — Design

## Decisions

- **The marker gains one field, `last_alert`** (ISO-8601 UTC, written by `_now()`), beside
  `since` and `alerted` — the issue's own proposal; the marker stays the only state, so no
  second file to keep in step with it.
- **`AUTH_DARK_REALERT_HOURS` is a module constant in `dispatcher/main.py`** next to
  `AUTH_DARK_GRACE_MINUTES` — not configurable (proposal non-goal); changing it is a code change.
- **The interval is measured from the last alert, not from `since`** — alerts land at
  grace + k·interval (0.5 h, 4.5 h, …) whatever the pass cadence; a late pass delays only that
  reminder, it never shifts later ones earlier.
- **`minutes` stays the age of `since`** — every reminder says how long the box has been dark,
  so a reader who saw only the latest message still sees the whole outage.
- **A legacy marker (`alerted` true, no `last_alert`) reads `last_alert` as `since`** — a box
  mid-incident at deploy re-alerts within at most one interval instead of alerting at once or
  never; costs one branch.
- **`alerted` false ignores any `last_alert`** — the grace path decides the first alert; a stray
  field cannot skip the grace.
- **Send first, then write the marker** (as today) — a `send` that raises leaves the marker
  as it was, so the next pass retries; a write that fails after a send can repeat one alert on
  the next pass, the cheaper failure.
- **An alerted marker is parsed before it returns** — today `alerted` true returns before `since` is read, so a corrupt alerted marker was never repaired; now it is (req 8).
- **An unparseable `last_alert` (on an alerted marker) resets the marker like an unparseable `since`** — one repair
  path; the price is that the incident restarts its grace (≤ 30 min of silence) and `minutes`
  restarts from the repair.
- **A `last_alert` in the future (clock stepped back) gets no special case** — the reminder is
  delayed until the clock passes it; the box's clock is NTP-synced, so the delay is bounded by
  the step.

## Data model

No database. The one persisted record is the JSON marker `<state_dir>/auth-dark`:

| Table | Field | Constraint | Why |
|---|---|---|---|
| `auth-dark` (file) | `since` | required, ISO-8601 with offset; unparseable → marker reset | incident start; source of `minutes` |
| `auth-dark` (file) | `alerted` | bool; false until the first alert | grace gate (req 1–2) |
| `auth-dark` (file) | `last_alert` | new; absent until the first alert, then ISO-8601 with offset; read only when `alerted` is true: unparseable → marker reset, absent → read as `since` | re-alert clock (req 3–4, 7) |
| `auth-dark` (file) | (whole file) | exists only while dark; deleted by the first non-dark pass | recovery forgets the incident (req 6) |

The file has no constraint engine: every row above is enforced in `_auth_dark_edge`, and each
has a scenario in `spec.md`.

## Identities

None. No key is widened: the marker stays one file per box at `<state_dir>/auth-dark`.

## Seams

- `main.run_pass(cfg, deps)` with `main.fetch_all` patched — existing; the seam of the
  current auth-dark tests in `tests/test_main.py`. Observed through `deps.notifier.sent`, the
  `auth_dark` call's kwargs, and the marker file. Elapsed time is simulated by back-dating
  `since` and `last_alert` in the marker.
- `main.AUTH_DARK_REALERT_HOURS` — new module constant the tests read to build back-dated
  markers, as they read `main.AUTH_DARK_GRACE_MINUTES` today.
