# Claim forecast reads the dispatcher's quarantine verdict — Spec

## Requirements

1. **Verdict file.** Each dispatcher pass that reaches `_claim_new` replaces
   `<state_dir>/quarantine-verdict.json` whole, written with
   `state.write_json_atomic`, with content
   `{"pass_started": "<same string as pass.json started_at>", "skipped": [["alpha", 73], ["beta", 12]]}`.
   `skipped` holds `[repository_name, issue]` for every candidate that
   `check_quarantine` returned True for during this pass — this includes the
   unreadable-record, no-blocker and `gh`-failed cases, because the dispatcher
   skips those too — sorted, without duplicates.
2. **Collection.** `_claimable` takes an extra `skipped: set[tuple[str, int]]`
   parameter and adds `(repository.name, cand.number)` to it right before the
   `continue` after `check_quarantine`. `_claim_new` creates the set, passes it
   to every generator, and writes the file in a `finally:` so every exit path
   writes it: the `free <= 0` early return (empty list), `pick_project`
   returning None, and the return on slot exhaustion.
3. **Writer helper.** `failures.write_quarantine_verdict(state_dir,
   pass_started, skipped)` lives next to `quarantine_path`; it sorts the list,
   calls `write_json_atomic`, and catches `OSError` with a stderr warning. It
   never raises into the pass (same contract as `_write_heartbeat`).
4. **Clearing.** An entry disappears the first pass after its record is
   removed or its blocker closes, because the file is replaced whole. When
   `claims_paused` skips `_claim_new`, the file is not rewritten; the reader
   treats it as stale (requirement 5).
5. **Reader.** `Sources.quarantine_verdict(heartbeat) -> frozenset[tuple[str, int]]`
   in `web/sources.py` returns an empty set when the file is missing,
   unreadable (`OSError`), torn or invalid UTF-8 / invalid JSON (`ValueError`),
   not a JSON object, has no list `skipped`, or when its `pass_started`
   differs from `heartbeat["started_at"]` (or `heartbeat` is None / has no
   `started_at`). Entries that are not a two-item `[str, int]` pair (a bool is
   not an int) are dropped individually. It never raises and does no I/O
   beyond reading that one file.
6. **Use.** The board route feeds `build_board(..., quarantined=...)` from
   `sources.quarantine_verdict(heartbeat)`, using the same heartbeat dict it
   passes as `heartbeat=` (one `pass_heartbeat()` read per request), instead
   of from `sources.quarantine_entries()`. `_queue_heads` keeps excluding
   `(repository, number)` pairs in `quarantined` from `mine`, keyed on the
   pair (alpha#73 never hides beta#73). If every ranked candidate of every
   project is quarantined, the verdict is `no-candidates`.
7. **Docstring.** The `next_claim` docstring no longer lists
   `check_quarantine` under "Gates it does NOT model". It states that
   quarantine is modelled from the dispatcher's verdict as of the last
   completed pass: a record written or a blocker closed after that pass shows
   up one pass later, and candidates the pass never reached (a project that
   got no turn because capacity ran out first, or whose board read failed)
   are not in the set, so the forecast treats them as claimable, as before.
8. **No live calls.** `/api/board` still returns 200, makes no `gh` call and
   starts no thread; the existing degrade tests pass unchanged.

## Scenarios

### Scenario: an open blocker lands in the verdict file
- **Given** a quarantine record `quarantine/alpha-73.json` with `blocker_issue` 70 in `alpha`, and the fake GitHub reports #70 `OPEN`; alpha's ranked candidates are #73
- **When** one dispatcher pass runs
- **Then** `quarantine-verdict.json` exists with `skipped == [["alpha", 73]]` and `pass_started` equal to `pass.json`'s `started_at`, and #73 is not claimed

### Scenario: a closed blocker clears the entry and is claimed in the same pass
- **Given** the state after the previous scenario, and the fake GitHub now reports #70 `CLOSED`
- **When** the next pass runs
- **Then** `check_quarantine` deletes the record, #73 is claimed in that pass, and the verdict file's `skipped` is `[]` with that pass's `started_at`

### Scenario: unreadable record and failed lookup are both listed
- **Given** `quarantine/alpha-73.json` holds invalid JSON, and `quarantine/beta-12.json` names a blocker whose `issue_state` raises
- **When** a pass runs with alpha#73 and beta#12 ranked and capacity 2
- **Then** `skipped == [["alpha", 73], ["beta", 12]]` (sorted)

### Scenario: early return still writes the file
- **Given** capacity is already full, so `claims.claim_room` returns 0
- **When** a pass runs
- **Then** `quarantine-verdict.json` exists with `skipped == []` and this pass's `pass_started`

### Scenario: slot exhaustion still writes what was collected
- **Given** alpha#73 is quarantined (blocker OPEN), alpha#74 is claimable, and `allocate_slot` returns None
- **When** `_claim_new` runs
- **Then** it returns without claiming and the file lists `["alpha", 73]`

### Scenario: claims paused leaves the file untouched
- **Given** a verdict file from an earlier pass and a pending triage request (`claims_paused`)
- **When** a pass runs
- **Then** the verdict file is unchanged (its `pass_started` now differs from `pass.json`'s `started_at`)

### Scenario: unwritable verdict file never breaks the pass
- **Given** `quarantine-verdict.json` cannot be written (`write_json_atomic` raises `OSError`)
- **When** a pass runs
- **Then** a `[warn]` line naming the verdict write is printed to stderr, the pass completes, and `pass.json` is written

### Scenario: quarantined head is skipped for the next candidate (pure)
- **Given** alpha's queue is #73 then #74, fresh heartbeat, capacity 3, `quarantined={("alpha", 73)}`
- **When** `next_claim` runs
- **Then** the verdict is `will-claim` for alpha #74

### Scenario: only candidate quarantined (pure)
- **Given** alpha's queue is only #73 and `quarantined={("alpha", 73)}`
- **When** `next_claim` runs
- **Then** the verdict is `no-candidates`

### Scenario: same number in another repository is not excluded (pure)
- **Given** alpha's queue is #73 and `quarantined={("beta", 73)}`
- **When** `next_claim` runs
- **Then** the verdict is `will-claim` for alpha #73

### Scenario: reader returns the pairs of a matching file
- **Given** heartbeat `{"started_at": "2026-11-15T10:00:00+00:00", ...}` and a verdict file with the same `pass_started` and `skipped [["alpha", 73], ["beta", 12]]`
- **When** `quarantine_verdict(heartbeat)` runs
- **Then** it returns `frozenset({("alpha", 73), ("beta", 12)})`

### Scenario: reader degrades to an empty set
- **Given** each of: no file; bytes `b"\xff\xfe torn"`; invalid JSON; JSON `null`; JSON `[]`; `skipped` not a list; a `pass_started` of `"2026-11-15T09:50:00+00:00"` against heartbeat `started_at` `"2026-11-15T10:00:00+00:00"`; heartbeat `None`
- **When** `quarantine_verdict(heartbeat)` runs
- **Then** it returns `frozenset()` without raising

### Scenario: wrong-typed entries are dropped one by one
- **Given** a matching file with `skipped [["alpha", 73], ["beta", "12"], ["gamma", true], [1, 2], ["delta"], "x"]`
- **When** `quarantine_verdict(heartbeat)` runs
- **Then** it returns `frozenset({("alpha", 73)})`

### Scenario: board route uses the verdict, not the records
- **Given** fake sources with a fresh heartbeat (`started_at` S), alpha queue #212, #213, a quarantine record for alpha#212, and a verdict for S with `skipped []` (its blocker closed since the record was read)
- **When** `GET /api/board`
- **Then** `next_claim` is `will-claim` #212; and with the verdict `skipped [["alpha", 212]]` it is `will-claim` #213

### Scenario: board route stays offline
- **Given** the existing `/api/board` degrade tests (corrupt events, corrupt state) with a real `Sources`
- **When** they run
- **Then** they pass unchanged: status 200, no `gh` call, no thread started

### Scenario: docstring
- **Given** `web/read_model.py::next_claim.__doc__`
- **When** it is read
- **Then** the "Gates it does NOT model" list does not mention `check_quarantine`, and the docstring names the one-pass lag and unreached candidates

## Reconciliation with the code

The issue's line numbers were as of `61dbbdb`. What moved since, and how the
settled design absorbs it:

- **Commit `2237482` (2026-11-09) already threads `quarantined`** through
  `build_board`, `next_claim` and `_queue_heads` (`Collection[tuple[str, int]]`,
  keyed on the pair), but `web/app.py`'s board route fills it from
  `sources.quarantine_entries()` — record exists means blocked, which the
  decision forbids. The parameters stay; only the feed changes to
  `sources.quarantine_verdict(heartbeat)` (requirement 6), and the docstring
  paragraph that describes the record-based skip is rewritten (requirement 7).
  The tests in `tests/test_next_claim_shared_gate_acceptance.py` that drive
  the pure `quarantined=` argument stay valid;
  `test_board_route_skips_a_quarantined_head` is rewritten to feed the
  verdict, and `tests/webfakes.py::FakeSources` gains `quarantine_verdict`.
- **`_claimable` now has two early returns** before any candidate is
  evaluated (`_no_task_tokens`, and `BoardError`/`HostWait` from
  `candidates`). Those projects add nothing to `skipped`; that is the "never
  reached" case of requirement 7.
- **`_claim_new` now has a third exit**, `pick_project` returning None; the
  `finally:` covers it with the others.
- **`_claim_new` already receives `pass_started`** from `_run_pass`, the
  same value later written to `pass.json`'s `started_at`, so the helper needs
  no new plumbing for it.
- **`Sources.pass_heartbeat`** is a method on the `Sources` class (as is
  `quarantine_entries`), so the reader is a `Sources` method too.
- `pass.json` is written at the very end of a pass, after `_claim_new`. While
  a pass runs (or if it crashes after `_claim_new`), the verdict is newer than
  the heartbeat, the stamps differ and the forecast falls back to no
  quarantine skipping — the same as before this change, never a wrong skip.

## Out of scope

- `MAX_SLOTS` / `allocate_slot` in the forecast.
- Same-pass capacity used by `_resume_woken` / `_spawn_feedback`; the new file
  leaves room for that field later.
- Ghost cards and queue rows still show quarantined issues as queued.
- Changes to `check_quarantine`, the record format, `/failures`, or
  `Sources.quarantine_entries()`.
