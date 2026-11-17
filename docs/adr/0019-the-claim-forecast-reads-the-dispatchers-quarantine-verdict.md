# 0019. The claim forecast reads the dispatcher's quarantine verdict

Date: 2026-11-15

## Context

Only `failures.check_quarantine` knows whether a quarantined candidate is skipped, because it looks up the blocker live with `gh`. PR #79 ran that lookup in the web process and was reverted, after a wedged `gh` piled up live subprocesses and blocked the threadpool. Commit `2237482` then skipped every candidate that had a local record. The operator rejected that "record exists means blocked" rule on 2026-11-05: a record whose blocker has closed keeps hiding a candidate that the dispatcher would claim.

## Decision

Every pass that reaches `_claim_new` replaces `<state_dir>/quarantine-verdict.json` whole, in a `finally:`. The file holds `pass_started` and the sorted `[repository, issue]` pairs that `check_quarantine` skipped. `failures.write_quarantine_verdict` writes it and turns an `OSError` into a `[warn]`. `failures.quarantine_verdict_path` is the only spelling of the filename.

The console reads the file with `Sources.quarantine_verdict(heartbeat)`. It trusts the file only when `pass_started` equals the `started_at` of the same `pass.json` read the board shows. Anything else counts as no quarantine skipping. `/api/board` never calls `gh` and never derives quarantine from the records.

## Consequences

The forecast is one pass behind: a blocker that closes, or a record written, after the last completed pass shows up a pass later. Candidates the pass never reached are not in the set, so the forecast treats them as claimable. Between `_claim_new` and the heartbeat write, and after a pass that skipped claims, the stamps differ and the forecast skips no quarantined head. Later fields for the forecast, such as same-pass capacity, go into this file as new keys. The reader ignores keys it does not know.
