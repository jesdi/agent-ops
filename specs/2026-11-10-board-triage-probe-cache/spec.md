# Cache the herdr triage probe behind /api/board — Spec

Source: issue #89 (rewritten 2026-11-05 in triage, line numbers as of `61dbbdb`). The issue body
carries a settled design; this spec carries it over and reconciles it with `main` at `0bf826c`.

## Settled design (from the issue, carried over)

In `web/sources.py`:

1. Add `TRIAGE_TTL_SECONDS = 5.0` next to the other TTL constants.
2. Add `self._triage_running: tuple[float, bool] | None = None` in `__init__` (timestamp, value).
3. In `triage_state`, cache only the `triage.running()` result (the subprocess part). If a cached
   value exists and `self._clock() - at < TRIAGE_TTL_SECONDS`, reuse it; otherwise probe and
   store. A probe that raises is stored as `False` with the current timestamp, so a wedged server
   costs at most one slow probe per TTL, not one per request.
4. Keep `triage.load_request(self.state_dir)` outside the cache: it is a local file read, so a new
   request file still shows up on the next board load. Keep its existing
   `except Exception: paused = running` fallback.
5. Update the docstring: one probe per TTL window, degrade to `(False, False)` unchanged.

Use the injected `self._clock`. No threads, no background refresh, no async changes. No changes
to `app.py`, `triage.py` or `herdr.py`.

## Reconciliation with the current code (`0bf826c`)

Every assumption the design rests on still holds; only line numbers moved.

- `web/sources.py`: `RANK_TTL_SECONDS` / `DESCRIPTION_TTL_SECONDS` still at lines 25-26;
  `Sources.__init__` at line 52 stores `self._clock = clock` (line 60) and the cache dicts
  `_rank_cache` / `_desc_cache` (lines 62-63). `rank_rows` (line ~133) and `issue_description`
  (line ~151) still use the `now = self._clock()` / `now - cached[...] < *_TTL_SECONDS` pattern —
  strict `<`, which this change copies.
- `Sources.triage_state` moved from line 261 to line 260; body unchanged: `try: running =
  triage.running() except Exception: running = False`, then `paused = running or
  triage.load_request(self.state_dir) is not None` with `except Exception: paused = running`.
- `web/app.py` `board` still calls `sources.triage_state()` at line 333 and is still a sync `def`.
  It is still the only production caller. `web/__main__.py:40` builds one `Sources` per process,
  so the per-instance cache is a per-process cache. `/api/board/snapshot` must not call
  `triage_state` (asserted in `tests/test_web_routes_read.py`); unaffected.
- `dispatcher/triage.py`: `running()` (line 73) still calls `herdr.Tab.find(TAB_LABEL)`;
  `REQUEST_FILE = "triage-request.json"` (line 24) and `load_request` (line 84) read
  `<state_dir>/triage-request.json` and return `None` when absent or malformed.
- `dispatcher/herdr.py`: `TIMEOUT = 30` (line 21), `Tab.find` (line 249). Unchanged.
- `tests/test_web_sources.py`: `FakeClock` (line 60, mutable `.t`), `make_sources(tmp_path,
  github=..., sessions=..., clock=...)` (line 68) and `test_rank_rows_cached_within_ttl`
  (line 96) are as described. `make_sources` builds a fresh `Sources` per call, so the four
  existing `triage_state` tests (lines 451-492) each start with an empty cache and keep passing.
  `tests/webfakes.py` has its own fake `triage_state`, untouched.

Nothing moved underneath a settled decision; no decision needs revisiting.

## Requirements

1. `Sources.triage_state()` calls `triage.running()` at most once while
   `self._clock() - <time of last probe> < TRIAGE_TTL_SECONDS`, and returns the same tuple for
   calls inside that window when the request file is unchanged.
2. Once `self._clock() - <time of last probe> >= TRIAGE_TTL_SECONDS`, the next call probes again
   and its result reflects the new probe value.
3. A probe that raises is cached as `running = False` stamped with the current clock, so the
   result degrades to `(False, False)` (no request file) and no further probe runs inside the TTL.
4. The triage request file is read on every call, not cached: with `running` cached, creating or
   removing `<state_dir>/triage-request.json` changes `claims_paused` on the next call without a
   new probe. The existing `except Exception: paused = running` fallback stays.
5. `TRIAGE_TTL_SECONDS = 5.0` is a module constant in `web/sources.py`, and the cache uses the
   injected `self._clock` (no `time.time` calls, no threads).
6. Existing behaviour stays: `pytest tests/test_web_sources.py tests/test_triage.py` and the repo
   gate (`make gate`) pass.

## Scenarios

All scenarios use `make_sources(tmp_path, clock=clock)` with `clock = FakeClock()` (t =
1 000 000.0) and `monkeypatch.setattr` on `dispatcher.triage.running` (the module `web.sources`
imports as `triage`) with a call counter. No request file exists unless a scenario says so.

### Scenario: two calls within the TTL probe once

- **Given** `running` returns `False` and counts calls
- **When** `triage_state()` is called, then `clock.t += 2`, then `triage_state()` is called again
- **Then** both calls return `(False, False)` and `running` was called exactly once

### Scenario: a call after the TTL probes again and reflects the new value

- **Given** `running` returns `False` on the first call and `True` afterwards, counting calls,
  and `triage_state()` has been called once (returns `(False, False)`)
- **When** `clock.t += TRIAGE_TTL_SECONDS + 1` and `triage_state()` is called
- **Then** it returns `(True, True)` and `running` was called twice

### Scenario: TTL boundary — exactly TRIAGE_TTL_SECONDS later re-probes

- **Given** `running` counts calls and `triage_state()` has been called once
- **When** `clock.t += TRIAGE_TTL_SECONDS` (exactly 5.0) and `triage_state()` is called
- **Then** `running` was called twice (the window is strict `<`, matching `rank_rows`)

### Scenario: TTL boundary — just inside the window reuses the cache

- **Given** `running` counts calls and `triage_state()` has been called once
- **When** `clock.t += TRIAGE_TTL_SECONDS - 0.1` and `triage_state()` is called
- **Then** `running` was called once

### Scenario: a failing probe degrades to False and is cached

- **Given** `running` raises `RuntimeError` and counts calls
- **When** `triage_state()` is called, then `clock.t += 1`, then `triage_state()` is called again
- **Then** both calls return `(False, False)` and `running` was called exactly once

### Scenario: a failing probe is retried after the TTL

- **Given** `running` raises on the first call and returns `True` afterwards, and
  `triage_state()` has been called once (returns `(False, False)`)
- **When** `clock.t += TRIAGE_TTL_SECONDS + 1` and `triage_state()` is called
- **Then** it returns `(True, True)` and `running` was called twice

### Scenario: a new request file shows without a new probe

- **Given** `running` returns `False` and counts calls, and `triage_state()` returned
  `(False, False)`
- **When** `<tmp_path>/triage-request.json` (path from `triage.REQUEST_FILE`) is written directly
  as `{"requested_at": "2026-08-01T00:00:00+00:00"}` — not via `triage.enqueue`, which calls
  `running()` itself and would skew the counter — and `triage_state()` is called within the TTL
- **Then** it returns `(True, False)` and `running` was called exactly once

### Scenario: a removed request file clears the pause without a new probe

- **Given** the request file exists, `running` returns `False` and counts calls, and
  `triage_state()` returned `(True, False)`
- **When** the file is removed (`triage.clear_request(state_dir)`) and `triage_state()` is called
  within the TTL
- **Then** it returns `(False, False)` and `running` was called exactly once

### Scenario: cached running=True still pauses claims

- **Given** `running` returns `True` and counts calls
- **When** `triage_state()` is called twice within the TTL
- **Then** both calls return `(True, True)` and `running` was called once

### Scenario: existing tests still pass

- **Given** the change is in place
- **When** `pytest tests/test_web_sources.py tests/test_triage.py` and `make gate` run
- **Then** both pass, including `test_triage_state_degrades_to_false`,
  `test_triage_state_probes_tmux_once`, `test_triage_state_request_file_pauses_claims` and
  `test_triage_state_running_pauses_claims_too`

## Out of scope

- Threading, background probes, or making `board` async.
- The dispatcher writing the triage flag to state.
- Changing `herdr.TIMEOUT`, `Tab.find`, or `triage.running` semantics.
- Caching `rank_rows`, `issue_description`, or other board data.
- Any edit to `web/app.py`, `dispatcher/triage.py`, `dispatcher/herdr.py`.
