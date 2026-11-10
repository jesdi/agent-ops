# Unreadable quarantine record — Proposal

## Why

A quarantine record under `state_dir/quarantine/{repository}-{issue}.json` is
read in three places, each with its own guard, and they disagree about what an
unreadable record means:

- `failures.check_quarantine` (dispatcher, every claim pass) treats invalid
  JSON as "still blocked", but its guard is `(json.JSONDecodeError, OSError)`
  and it calls `rec.get(...)` unchecked. A torn write (invalid UTF-8 raises
  `UnicodeDecodeError`, a `ValueError`) or valid JSON that is not an object
  (`null`, a list) raises instead. The raise is not caught anywhere between the
  `_claimable` generator and `_claim_new`, so the claim phase of the pass dies
  for every project, not just that issue — reproduced by
  `tests/test_quarantine_unreadable_acceptance.py` (74, 75: `AttributeError`;
  73: `UnicodeDecodeError`).
- `main._apply_retry_intent` has the same guard and the same unchecked
  `.get`, so retrying such a record raises too.
- `Sources.quarantine_entries` (console) reads with `(OSError, ValueError)`
  and **skips** every unreadable or non-object record (restored by the revert
  of PR #79, which had surfaced them as blank rows indistinguishable from real
  ones). Because `/api/failures`, the retry route's "is this quarantined?"
  check and the board's claim forecast all read from it, an unreadable record
  is invisible on the Failures page, cannot be retried from the console (404),
  and the forecast names its issue as the next claim although it never will be.

The root cause is the missing explicit state: a record is either readable or
unreadable, and nothing carries that second state. The operator sees a task
that never gets claimed and no reason why.

## For whom

The operator, reading the console's Failures page and the board, and the
dispatcher's claim pass.

## Goal

An unreadable quarantine record keeps blocking its issue without crashing any
pass, is listed on `/api/failures` explicitly marked unreadable
(`readable: false`), renders in `FailureList` as "quarantine record unreadable —
still blocking" with a working Retry, and counts as quarantined in the claim
forecast.

## Non-goals

- Repairing or rewriting a corrupt record: the operator clears it with Retry
  (or by deleting the file), as today.
- Readable records with a missing blocker (`blocker_issue` 0): unchanged.
- Files under `quarantine/` whose name is not `{repository}-{issue}.json`: no
  claim pass ever reads them, so they block nothing and stay unlisted.
- `agent-ops status` (`web/box_status.py`): it already reports unreadable
  records as "unknown (unreadable quarantine/<name>)".
- Fingerprint files under `failures/`.
