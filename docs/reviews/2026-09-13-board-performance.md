# Board response-time investigation

Read-only investigation of the running box on 2026-09-13. Measurements used
loopback HTTP over the existing SSH alias, so they exclude phone/network delay.
No deployment or box configuration changes were made.

## Measured behavior

| Probe | Result |
| --- | ---: |
| Deployed revision | `02f4948` (PR #114) |
| `/board` HTML | 4.9 ms |
| `/api/board/snapshot` | 404 |
| Cold `/api/board` | 6.660 s |
| Immediate repeated `/api/board` | 18.8 ms |
| `/api/budget` | 3.8 ms |
| Direct triage session probe | 3.4 ms |
| Cold `/api/queue`, then `/api/board` | 5.004 s, then 26 ms |

The deployed build lacks PR #115, which is present on `origin/main` at
`028d848` and included in PR #116's base. That change adds a local-only board
snapshot endpoint and renders saved cards while the full request runs.
Deploying a build containing #115 is the first recommended action: it should
remove the blank initial board without waiting for live ranking data.

## Remaining bottleneck

`Sources.rank_rows` performs synchronous ranking on a 15-second cache miss.
The deployed box has one target, `portfolio_eval`; its external rank script
sequentially requests `gh project item-list --limit 200` and
`gh issue list --state all --limit 500`. The cold queue request warms the same
ranking cache used by the board, after which the board responds in 26 ms.
This identifies ranking as the principal observed delay. Parallelizing across
multiple targets alone would not help this single-target deployment.

A controlled local experiment against the latest PR injected 450 ms of live
source latency: snapshot returned in 43 ms, versus 518 ms for the full board.
Four concurrent cold ranking requests launched four underlying fetches; cache
refresh is not currently shared among concurrent callers.

## Recommended follow-up

Use a bounded ranking cache with one refresh per target. Return existing rows
immediately as stale while refreshing, retain them on failure with retry
backoff, and invalidate board SSE subscribers when a refresh completes.
Successful queue mutations should explicitly invalidate rankings. Mutation
lookups must continue requiring fresh data; stale display rows must not
authorize queue changes. Keep the existing snapshot/explicit loading behavior
for a truly cold cache rather than presenting an empty cache as no candidates.

Behavioral tests should cover concurrent cold/expired requests, stale response
latency, refresh completion notification, failure backoff, fresh mutation
requirements, post-mutation invalidation, and bounded worker shutdown.

## Other observations to investigate

- `/api/budget` and `/api/board` can independently refresh an expired usage
  cache. Failed OAuth/ccusage results are not cached. This can cause outliers,
  but it was not the measured bottleneck.
- Deployed `/api/queue` returned HTTP 500 both cold and warm. The exception
  was not diagnosed in this investigation; treat this as a separate open bug.
  Automatic approval review rejected reading recent service logs because they
  could contain sensitive request/session data. No logs were exported. The
  queue timing includes a failed render; the successful cold/warm board
  measurements are the primary evidence.
