# Cache the herdr triage probe behind /api/board — Design

## Decisions

- **Cache only the `triage.running()` result, per `Sources` instance** — the board stays correct for the request file (a local read) and only the slow subprocess is skipped; one `Sources` per process makes it a per-process cache.
- **Store `(timestamp, bool)` in one attribute, `None` until the first probe** — no lock: two concurrent first requests may both probe once, which is bounded and was ruled out of scope (no threading work).
- **A raising probe is stored as `False` with the current timestamp** — a wedged server costs one slow probe per TTL, at the price of a failed probe hiding a real triage tab for up to 5 s.
- **Window is strict `<` on the injected `self._clock`** — same pattern as `rank_rows` and `issue_description`; tests drive it with `FakeClock`.
- **`TRIAGE_TTL_SECONDS = 5.0` as a module constant beside the other TTLs** — not config: the value never varies.
- **No change to `app.py`, `triage.py`, `herdr.py`** — the cache sits in the one production caller's dependency.

## Data model

None. No persisted state; the cache is an in-memory attribute.

| Table | Field | Constraint | Why |
|---|---|---|---|
| — | — | — | — |

## Identities

None.

## Seams

- `Sources.triage_state()` in `web/sources.py` — existing. Tests monkeypatch `dispatcher.triage.running` with a call counter and drive time through `FakeClock` from `make_sources`.
