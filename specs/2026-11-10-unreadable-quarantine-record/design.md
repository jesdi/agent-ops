# Unreadable quarantine record — Design

## Decisions

- **One reader for a quarantine record, in `dispatcher/failures.py`** — `check_quarantine`,
  `main._apply_retry_intent` and `Sources.quarantine_entries` all read through it, so the
  three guards can no longer disagree; costs the console one more import from `dispatcher`
  (it already imports `claims`, `intents`, `state`, …).
- **The reader never raises and returns one of three states: gone, unreadable, or the record
  (a JSON object)** — "gone" is a `FileNotFoundError` on read (deleted after the `exists()`
  check or the directory scan); every other `OSError`, any `ValueError` (invalid UTF-8,
  invalid JSON) and any non-object JSON is "unreadable". Callers branch on the state, not on
  exceptions; costs a small result type instead of a bare `dict`.
- **Unreadable fails closed in the dispatcher** — `check_quarantine` returns `True` and leaves
  the file; only Retry (or deleting the file) clears it. Same as today's intent, now without
  the crash.
- **Retry of an unreadable record unlinks it and clears no fingerprint** — there is no
  readable fingerprint to clear; a stale fingerprint marker at worst dedupes the next
  identical failure report, as for any record retried today.
- **`QuarantineEntry.readable: bool` is required, no default** — every producer (real
  `Sources`, `FakeSources` rows in tests, frontend fixtures) has to state it; costs touching
  the existing fakes and fixtures once.
- **An unreadable entry keeps the existing field types with blank values (`""`, `0`) and
  `readable: false`** — the frontend branches on `readable`, never on blank values; making
  every field optional instead would ripple `| null` through the generated types and every
  reader of `FailuresView`.
- **No new blocker-lookup guard** — an unreadable entry carries `blocker_issue: 0`, which the
  `/api/failures` route already maps to `blocker_open: null` without calling `issue_open`.
- **Retry route and claim forecast are not changed** — both already read
  `Sources.quarantine_entries`; listing unreadable records fixes them.
- **The unreadable row is its own render branch in `FailureList`** — label
  `{repository}#{task_issue}`, text "quarantine record unreadable — still blocking", same
  Retry button, busy key and error slot (`${repository}#${task_issue}`, ADR 0011); no
  fingerprint, blocker text, blocker chip or relative time.

### Red-team findings left out of scope

- `_apply_retry_intent(cfg, issue)` takes no repository: a retry of `alpha#74` also deletes
  `beta-74.json`. Pre-existing, independent of readability (open question on the review page).
- A directory named `alpha-7.json` is unreadable (blocks, listed), but Retry's unlink raises
  `IsADirectoryError`. No writer creates directories there.
- A readable object with ill-typed fields (`{"blocker_issue": "x"}`) fails `QuarantineEntry`
  validation and 500s `/api/failures`. Pre-existing; the spec's non-goals keep readable
  records unchanged.
- `web/box_status.py: _quarantine` (`agent-ops status`) keeps its own readability check and
  reports a vanished file as unreadable for one run; the spec keeps `agent-ops status` out of
  scope, so it is left alone on purpose.
- `write_quarantine` is not atomic: a console poll during the write can show a transient
  unreadable row. It fails safe (the dispatcher keeps blocking) and the next poll corrects it.

## Data model

No database. The record is a file `state_dir/quarantine/{repository}-{issue}.json`; the
invariants live in the shared reader and the API model.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `quarantine/{repository}-{issue}.json` | file name | identity: `(repository, task_issue)` comes from the name only | the key `check_quarantine` and the retry route use |
| `quarantine/*.json` | body | a JSON object, else unreadable | `rec.get` on `null`/a list crashed the claim pass |
| `QuarantineEntry` (API) | `readable` | `bool`, required | an unreadable row is explicit, never a blank row |
| `QuarantineEntry` (API) | `blocker_open` | `null` when `readable` is false | no lookup for a record with no blocker |

## Identities

None. The key `(repository, issue)` from the filename is unchanged.

## Seams

- `failures.check_quarantine(state_dir, github, repository, issue)` — existing.
- `main.run_pass(cfg, deps)` with a `retry` intent and with Ready candidates — existing
  (`tests/test_main.py` harness).
- `GET /api/failures`, `POST /api/task/{repository}/{issue}/retry`, `GET /api/board` against
  the real `Sources` — existing (`tests/test_quarantine_unreadable_acceptance.py` pattern).
- `FailureList` / `FailuresPage` rendered with the fixtures — existing
  (`frontend/src/pages/__tests__/FailuresPage.test.tsx`).
