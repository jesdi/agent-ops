# Unreadable quarantine record — Spec

An unreadable quarantine record is any `quarantine/{repository}-{issue}.json`
whose read fails (`OSError` other than the file being gone), whose bytes are
not UTF-8, whose text is not JSON, or whose JSON is not an object (`null`, a
list, a number, a string). Any JSON object, even `{}`, is readable. A record
that vanishes between listing the directory and reading it (a retry or a
closed blocker deleted it) is gone, not unreadable: it blocks nothing.
Its identity (`repository`, `task_issue`) always comes from the filename, the
same key `check_quarantine` and the retry route use.

## Requirements

1. `GET /api/failures` lists every unreadable record as a `QuarantineEntry`
   with `repository` and `task_issue` from its filename, `readable: false` and
   `blocker_open: null`, and makes no blocker-state lookup for it.
2. `QuarantineEntry` gains a required `readable: bool`; every readable record
   is listed exactly as today plus `readable: true`. The regenerated
   `frontend/src/lib/api-types.ts` carries the field (the gate's
   `gen:api` freshness check passes).
3. An unreadable record whose filename does not parse as
   `{repository}-{issue}` is not listed (it blocks nothing), and neither is
   a record that is gone by the time it is read.
4. `failures.check_quarantine` returns `True` (still blocked) for every
   unreadable record and never raises; it leaves the file in place.
5. `POST /api/task/{repository}/{issue}/retry` returns 202 for an issue whose
   record is unreadable.
6. The dispatcher's retry intent on an unreadable record deletes the record
   without raising, so the issue is claimable on the next pass.
7. The board's claim forecast treats an issue with an unreadable record as
   quarantined: it is never named as the next claim.
8. `FailureList` renders an unreadable entry as a row labelled
   `{repository}#{task_issue}` that reads "quarantine record unreadable — still
   blocking", with the Retry button and the same per-row busy/error behaviour
   as other rows; it shows no fingerprint, blocker, blocker-state chip or
   relative time (no blank or zero values).
9. The frontend test fixtures include an unreadable entry, and a test covers
   the render branch of requirement 8.

## Scenarios

### Scenario: reproduction — unreadable records are hidden and crash the pass

This is the reproduction committed as
`tests/test_quarantine_unreadable_acceptance.py`; today it fails.

- **Given** `quarantine/` holds `alpha-73.json` = bytes
  `{"blocker_issue": \xff\xfe}` (torn write), `alpha-74.json` = `null`,
  `alpha-75.json` = `[1, 2]`, `alpha-76.json` = `{not json`, and a readable
  `alpha-77.json` written by `write_quarantine("alpha", 77, "r", 5, "fp")`
- **When** the operator calls `GET /api/failures` against the real `Sources`
- **Then** the response is 200 and `quarantined` holds task issues 73, 74, 75,
  76 and 77; 73–76 each have `repository: "alpha"`, `readable: false`,
  `blocker_open: null`; 77 has `readable: true`
  (today: only 77 is listed)
- **And when** `check_quarantine(state_dir, gh, "alpha", n)` runs for n in
  73–76 **then** it returns `True` for each (today: 73 raises
  `UnicodeDecodeError`, 74 and 75 raise `AttributeError`)
- **And when** the operator posts `/api/task/alpha/74/retry` **then** it is
  202 (today: 404 "issue alpha/74 is not quarantined")

### Scenario: a readable record is unchanged except for the flag

- **Given** only `alpha-77.json` from `write_quarantine("alpha", 77,
  "jesdi/alpha", 5, "fp")` with blocker #5 open
- **When** `GET /api/failures`
- **Then** its entry is `{repository: "alpha", task_issue: 77, blocker_repo:
  "jesdi/alpha", blocker_issue: 5, fingerprint: "fp", created_at: <written>,
  blocker_open: true, readable: true}`

### Scenario: an empty object is a readable record (boundary)

- **Given** only `alpha-78.json` = `{}`
- **When** `GET /api/failures`
- **Then** its entry has `task_issue: 78`, `readable: true` and
  `blocker_open: null` — listed as today, not as unreadable

### Scenario: the generated API types carry the flag

- **Given** `QuarantineEntry` with its `readable` field
- **When** the gate regenerates `frontend/src/lib/api-types.ts` (`pnpm gen:api`)
- **Then** the file is unchanged against the committed one, and its
  `QuarantineEntry` has `readable: boolean`

### Scenario: no blocker lookup for an unreadable record

- **Given** only `alpha-74.json` = `null`
- **When** `GET /api/failures`
- **Then** `issue_open` is never called and the entry has `blocker_open: null`

### Scenario: an unreadable record with an unparseable name is not listed

- **Given** `quarantine/broken.json` = `null` and readable `alpha-77.json`
- **When** `GET /api/failures`
- **Then** `quarantined` holds only task issue 77

### Scenario: a record gone before it is read is not listed

- **Given** `quarantine/alpha-79.json` is listed by the directory scan but
  deleted before its read (the read raises `FileNotFoundError`), and readable
  `alpha-77.json`
- **When** `GET /api/failures`
- **Then** `quarantined` holds only task issue 77, and the response is 200

### Scenario: an unreadable record never crashes the claim pass

- **Given** project with repositories `alpha` and `beta`, Ready candidates
  `alpha#74` and `beta#10`, one free slot each, and `quarantine/alpha-74.json`
  = `null`
- **When** a dispatcher pass runs
- **Then** the pass completes without raising, `alpha#74` is not claimed, its
  record is still on disk, and `beta#10` is claimed

### Scenario: retrying an unreadable record clears it

- **Given** `quarantine/alpha-75.json` = `[1, 2]`
- **When** the dispatcher applies a `retry` intent for `alpha#75`
- **Then** no exception is raised, `alpha-75.json` is gone, and the next
  `check_quarantine("alpha", 75)` returns `False`

### Scenario: the claim forecast skips an unreadable head

- **Given** `alpha`'s queue head is #212 and `quarantine/alpha-212.json` =
  `{not json`, with #213 next in the queue
- **When** `GET /api/board`
- **Then** the next-claim forecast names #213, not #212

### Scenario: FailureList renders an unreadable entry explicitly

- **Given** `failures.quarantined` = `[{repository: "widget", task_issue: 40,
  readable: false, blocker_repo: "", blocker_issue: 0, fingerprint: "",
  created_at: "", blocker_open: null}]`
- **When** `FailureList` renders
- **Then** the row `widget#40` shows "quarantine record unreadable — still
  blocking" and a Retry button, and shows no "blocker #0", no "blocker state
  unknown" chip and no relative time
- **And when** the operator clicks Retry **then** `onRetry("widget", 40)` is
  called, and while `widget#40` is busy the button is disabled
- **And when** the retry fails with "issue widget/40 is not quarantined"
  **then** that error shows inside the `widget#40` row

### Scenario: readable rows render as before

- **Given** the existing fixture entry `widget#38` (now with `readable: true`)
- **When** `FailureList` renders
- **Then** it shows fingerprint, `blocker #39 (jesdi/widget)`, the "blocker
  open" chip and the relative time, and no "unreadable" text

## Out of scope

Repairing corrupt records; readable records with no blocker; files whose name
is not `{repository}-{issue}.json`; `agent-ops status`; fingerprint files. The
existing tests that assert the skip (`test_quarantine_entries_skip_an_unreadable_record`
in `tests/test_web_sources.py`, `test_failures_degrades_to_200_on_corrupt_quarantine_record`
in `tests/test_web_routes_read.py`) are superseded by requirement 1 and must be
updated, not kept.
