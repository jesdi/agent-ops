# Claim forecast reads the dispatcher's quarantine verdict

## Why

The console header forecasts which issue the dispatcher claims next. Whether a
quarantined candidate is skipped is decided only by
`dispatcher/failures.py::check_quarantine`, with a live `gh issue_state`
lookup of the blocker (a closed blocker deletes the record, so the candidate
is claimable again in the same pass). That lookup cannot run on the ~1s-polled
`/api/board` route: PR #79 mirrored it in the web process three times and was
reverted (40 live `gh` subprocesses under a wedged `gh`, threadpool blocked).

Since the issue was written, commit `2237482` made the forecast skip every
candidate that has a local quarantine record (`web/app.py` feeds
`quarantined` from `sources.quarantine_entries()`). That is the "record
exists means blocked" rule the operator rejected on 2026-11-05: a record whose
blocker already closed keeps hiding its candidate, while the dispatcher would
claim it. The dispatcher already computes the true answer each pass and throws
it away.

Operator decision (2026-11-05, option (a)): the dispatcher writes its own
quarantine verdict to state each pass, and the console's `next_claim` reads
that file. One source of truth; the read path stays pure filesystem. Do not
call `gh` from the web process, and do not use "record exists means blocked"
in the console.

## For whom

The operator watching the console header ("will claim #N").

## Goal

The claim forecast skips exactly the candidates the dispatcher's last
completed pass skipped as quarantined, read from a state file the dispatcher
writes, with no `gh` call and no thread on `/api/board`.

## Non-goals

- `MAX_SLOTS` / `allocate_slot` modelling in the forecast.
- Same-pass capacity consumed by `_resume_woken` / `_spawn_feedback` (a later
  field in the same file; separate ticket).
- Ghost cards and the other queued-issue views (`web/read_model.py`
  `build_board` ghosts, `web/app.py` queue rows) still show quarantined issues
  as queued; hiding or marking them is a product call.
- Any change to `check_quarantine`, the quarantine record format, the
  `/failures` page, or `Sources.quarantine_entries()` (it still feeds
  `/api/failures` and the retry route).
