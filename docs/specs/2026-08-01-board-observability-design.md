# Board observability: next-claim ETA, issue descriptions, real queue on the board

Date: 2026-08-01
Status: approved in design review; awaiting spec review

## Problem

Three observability gaps in the web console:

1. **No sense of when the system will act.** The dispatcher is a systemd timer
   pass every 10 minutes, and a task is only claimed on a pass when a capacity
   slot is free and budget allows — but the board shows none of this. The
   operator cannot tell whether the next task starts in 4 minutes or 4 hours,
   nor why it is waiting.
2. **Task descriptions live only on GitHub.** The task detail page never shows
   the issue body; understanding what a task *is* requires a round-trip to
   GitHub.
3. **The Queued column is a lie of omission.** It holds only claimed tasks
   whose spec session has not spawned (near-always empty), while the actual
   upcoming work — ranked, in consumption order — sits on a separate Queue
   page. The board does not answer "what will run, and in what order".

Additionally, nothing on the board says how long tasks take, so even a visible
queue gives no time expectation.

## Design

Four parts. All view logic lives in `web/read_model.py` (pure, unit-tested);
all disk/subprocess access in `web/sources.py`; dispatcher changes are minimal
appends to existing transition points.

### 1. Dispatcher heartbeat and next-claim ETA

**Dispatcher.** At the end of every pass, `dispatcher/main.py` atomically
writes `pass.json` to the state dir:

```json
{"started_at": "...", "finished_at": "...", "interval_minutes": 10}
```

`interval_minutes` comes from config (`Config.pass_interval_minutes`, default
10) so provision (`agent-ops-dispatcher.timer`) and code cannot drift silently
— the timer's `OnUnitActiveSec` and the config value are documented as paired
settings (a comment at each site pointing at the other).

**Read model.** New `NextClaimView` on the board payload:

- `next_pass_eta`: `finished_at + interval_minutes` (ISO timestamp; the client
  renders the countdown and clamps to "due now" when past).
- `verdict`, one of:
  - `will-claim` — free capacity on some target, budget `would_spawn` true,
    and at least one candidate exists. Carries `next_issue` + `next_target`.
  - `no-candidates` — capacity/budget fine, queue empty.
  - `capacity-full` — no target has a free capacity slot.
  - `budget-blocked` — carries the existing `minutes_to_reset`.
  - `unknown` — heartbeat missing or stale (older than 2 × interval); rendered
    as "dispatcher not running?".
- Verdict logic reuses `consumes_capacity()` / budget `would_spawn` — no
  duplicated policy; the read model consumes the same inputs the dispatcher
  does and mirrors `_claim_new`'s candidate filter (Ready + `auto` +
  unblocked + not in-flight, in rank order).

**Frontend.** A compact status line in the board header next to `BudgetBar`:
"Next pass in 6m — will claim #123", "Next pass in 6m — budget resets in
2h 10m", "Next pass in 6m — capacity full", or "Dispatcher not running?".
Countdown ticks client-side (1 s interval, zero extra requests; re-anchored on
every board refetch). The `next_issue` card gets a visually distinct **Next**
badge.

### 2. Issue description panel

- New endpoint `GET /api/task/{issue}/description` →
  `{title, body, url, fetched_at}`. `sources.py` shells out to
  `gh issue view <n> --repo <target-repo> --json title,body,url` with a
  subprocess timeout and an in-memory TTL cache (~5 min). Failure (gh error,
  timeout, unknown issue) returns an explicit error payload — never a 500,
  same pattern as `pane_history`. Works for any issue number, claimed or not.
- `TaskPage` gets a collapsible **Description** panel above the terminal,
  collapsed by default, fetched on first expand (no cost when unused),
  markdown-rendered like `SpecPanel`, `max-h` scrollable.
- Unclaimed (ghost) issues get a slim task view at the same `/task/:issue`
  route: title, this description panel, and the queue actions from part 3.
  The frontend decides slim vs full by whether `/api/task/{issue}` finds a
  task state.

### 3. Queued column becomes the real queue; Queue page retired

- `read_model.board()` merges the cached rank rows into the Queued column:
  claimed-but-unstarted tasks first, then a **ghost card** for every
  Ready + `auto` + unblocked, not-in-flight row, in rank order — exactly the
  set `_claim_new` would consume, in the order it would consume it.
- Ghost card fields: number, target, title, score, boost. Rendered muted with
  a dashed border; no stage/slot/model chips. Stale rank cache surfaces as a
  "stale" hint on the column, mirroring the old Queue page's `stale` flag.
- Ghost cards carry the **Boost / Demote / Next / Ready** actions as small
  buttons, reusing the existing `POST /api/queue/*` endpoints unchanged.
  Board SSE already invalidates on queue changes.
- Non-`auto` and blocked rows are excluded from the board: they will not be
  consumed as-is, and showing them would contradict "tasks that will be
  consumed". The Ready toggle remains reachable from the slim task view.
- Deleted: `QueuePage`, `QueueTable`, the `/queue` route and nav link.
  `GET /api/queue` and all `POST /api/queue/*` endpoints stay (the board
  consumes them; external scripts keep working).

### 4. Durations

All derived from `events.jsonl` — `claimed` (main.py:940), `stage-started`,
`parked`, `resumed`, `merged` — no TaskState migration. The log rotates at a
size cap, so every computation tolerates a missing head: absent events degrade
to "no duration shown", never a wrong number.

- **Active cards**: elapsed since the task's `claimed` event ("claimed 2h 15m
  ago"). **Done cards**: total claimed → merged duration.
- **Board header**: median claimed→merged over the last 20 merges (median, not
  mean — outliers), shown as "≈2h per task"; the same figure appears in ghost
  card tooltips as a rough expectation.
- **Task detail**: a per-stage timeline (spec 40m → plan 15m → implement
  1h 50m), with parked gaps attributed separately, computed from that issue's
  events. Folded into the `TaskDetail` payload as
  `timeline: [{label, seconds, kind}]` where `kind` ∈ {stage, parked} — no
  new endpoint.
- One pass over the bounded tail of `events.jsonl` (existing `events_tail`
  machinery); parsed in `sources`, shaped in `read_model`.

## Error handling

Every view degrades to an explicit state, never a blank or a 500:

| Failure | Surface |
|---|---|
| heartbeat missing / stale | verdict `unknown`: "dispatcher not running?" |
| rank cache stale | ghost cards shown with "stale" hint |
| `gh` failure / timeout | description panel shows a retryable error message |
| events missing (rotation, pre-feature tasks) | duration omitted |
| budget source unavailable | verdict `budget-blocked` with "unknown" detail, matching existing BudgetBar behaviour |

## Testing

House pattern:

- **pytest**: read-model unit tests for verdict logic (all five verdicts,
  staleness boundary), ghost-card merge/order, duration math (rotation-truncated
  logs, parked gaps, missing events); route tests via httpx fakes for
  `/description` (cache hit, gh failure); sources test for `pass.json`
  parsing; dispatcher test that a pass writes the heartbeat.
- **Vitest + MSW**: header status line (each verdict + ticking countdown),
  Next badge, ghost card rendering + actions, description panel
  (collapsed-by-default, lazy fetch, error state), slim task view.
- **Playwright**: board shows ghost card → open it → expand description →
  boost it → order changes.

## Out of scope

- Predicting *which pass* a deep-in-queue task will be claimed on (depends on
  future budget/capacity; only the head-of-queue gets a verdict).
- Persisting per-stage timestamps in TaskState (event log suffices).
- Changes to ranking or claiming policy — this is observability only.
