# A wrong-typed quarantine record no longer 500s /api/failures — Spec

## Requirements

1. `GET /api/failures` answers 200 when a readable quarantine record (valid
   JSON, an object) carries a field of the wrong type; the response lists
   every well-formed record and omits the malformed one.
2. `Sources.quarantine_entries()` treats a record as malformed, and skips it
   exactly as it skips an unreadable record, when any of these keys is
   present in its body with a value of another JSON type:
   - `task_issue`, `blocker_issue`: a JSON integer. A JSON boolean
     (`true`/`false`), a string (including a numeric one like `"75"`), a
     float, `null`, a list or an object is wrong-typed.
   - `blocker_repo`, `fingerprint`, `created_at`: a JSON string. A number,
     boolean, `null`, a list or an object is wrong-typed.
   The check applies to the body as written, before the filename overrides
   `task_issue`: a wrong-typed `task_issue` makes the record malformed even
   when the filename supplies the key.
3. A key that is absent still takes its existing blank default (`0` or
   `""`), and keys outside the five above are ignored as today: neither makes
   a record malformed.
4. A malformed record is skipped before anything downstream sees it:
   `/api/failures` makes no `issue_open` lookup for it.
5. The other consumers of `quarantine_entries()` see the same filtered list
   and do not fail: `/api/board` answers 200, and
   `POST /api/task/{repository}/{issue}/retry` for a malformed record's issue
   answers 404 "not quarantined", as it does for an unreadable record today.
6. Existing behaviour is unchanged: unreadable records (`OSError`,
   `ValueError` incl. invalid UTF-8) and non-object JSON (`null`, a list) are
   still skipped; the key still comes from the filename.

## Scenarios

### Scenario: reproduction — a wrong-typed field no longer 500s /api/failures

- **Given** `state_dir/quarantine/` holds `alpha-73.json` =
  `{"task_issue": 73, "blocker_repo": "r", "blocker_issue": "abc", "fingerprint": "f", "created_at": "c"}`,
  `alpha-74.json` = the same with `"blocker_repo": 5, "blocker_issue": 0`
  and `task_issue` 74, and `alpha-75.json` = a well-formed record with
  `task_issue` 75, `blocker_issue` 0
- **And** the app runs on real `Sources`
- **When** the operator calls `GET /api/failures`
- **Then** the response is 200 (today: 500)
- **And** `quarantined` lists exactly the issues `[75]`

### Scenario: wrong-typed integer fields are skipped

- **Given** quarantine records `alpha-80.json` .. `alpha-85.json`, each
  otherwise well-formed, whose `blocker_issue` is respectively `"abc"`,
  `"5"`, `true`, `1.5`, `null`, `[1]`
- **And** a well-formed `alpha-86.json` with `blocker_issue` 7
- **When** `quarantine_entries()` is read
- **Then** it returns exactly one entry, with `task_issue` 86 and
  `blocker_issue` 7

### Scenario: a wrong-typed task_issue is skipped even when the filename supplies the key

- **Given** `alpha-90.json` = a well-formed record except `"task_issue": "x"`
- **When** `quarantine_entries()` is read
- **Then** it returns no entry for issue 90

### Scenario: wrong-typed string fields are skipped

- **Given** three otherwise well-formed records `alpha-91.json`,
  `alpha-92.json`, `alpha-93.json` whose `blocker_repo`, `fingerprint` and
  `created_at` respectively are `5`, `null` and `{"at": 1}`
- **And** a well-formed `alpha-94.json`
- **When** `quarantine_entries()` is read
- **Then** it returns exactly the entry for issue 94

### Scenario: boundary — missing fields and extra keys are not malformed

- **Given** `alpha-95.json` = `{"blocker_repo": "r", "note": 42}` (no
  `task_issue`, `blocker_issue`, `fingerprint` or `created_at`; one extra
  non-string key)
- **When** `quarantine_entries()` is read
- **Then** it returns one entry with `repository` "alpha", `task_issue` 95,
  `blocker_repo` "r", `blocker_issue` 0, `fingerprint` "", `created_at` ""
- **And** `GET /api/failures` answers 200 listing issue 95 with
  `blocker_open` null

### Scenario: no GitHub lookup for a malformed record

- **Given** `alpha-73.json` with `"blocker_issue": "abc"` and a GitHub
  source that records every `issue_state` call
- **When** the operator calls `GET /api/failures`
- **Then** the response is 200 and `issue_state` was never called with
  `"abc"`

### Scenario: board and retry tolerate a malformed record

- **Given** only `alpha-73.json` with `"blocker_issue": "abc"`
- **When** the operator calls `GET /api/board`
- **Then** the response is 200
- **When** the operator calls `POST /api/task/alpha/73/retry`
- **Then** the response is 404 "issue alpha/73 is not quarantined"

### Scenario: existing skips still hold

- **Given** `alpha-73.json` = invalid UTF-8 bytes, `alpha-74.json` = `null`,
  `alpha-75.json` = `[1, 2]`, and a well-formed `alpha-76.json` whose body
  says `task_issue` 999
- **When** `quarantine_entries()` is read
- **Then** it returns exactly one entry, with `repository` "alpha" and
  `task_issue` 76 (the existing tests
  `test_quarantine_entries_skip_an_unreadable_record`,
  `test_quarantine_key_comes_from_the_filename_not_the_body` and
  `test_failures_degrades_to_200_on_corrupt_quarantine_record` stay green)

## Out of scope

Surfacing skipped records as an explicit "unreadable — still blocking" row
(issue #87); coercing wrong-typed values; `failures.check_quarantine` in the
dispatcher; `fingerprint_entries()`; any frontend or API schema change.
