# Provider priority: choose which subscription the box spends first — Design

## Decisions

- **One ordering point.** Every list the box routes goes through
  `dispatcher.models.candidates` (stage lists) or `triage_entry` (the triage list); both take an
  `order` and apply it before the review reorder — no call site orders entries itself, but
  every caller now has to build and pass the order.
- **The order is built once per pass from what the gate already has.** `priority.order(mode,
  usages, now, pace)` uses the same `usages`, `now` and `PaceConfig` the pass's `admit` uses, so
  ranking and admission never judge two different readings — the ranking is as old as the usage
  cache (up to 180 s).
- **Required pace lives in `dispatcher/usage.py`, beside allowance and headroom.** It is a new
  function over a `Window`; `allowance`, `readings`, `admits` and `Verdict` are not edited — the
  module gains a second number per weekly window that the gate never reads.
- **Session-bound is a provider-level verdict from the unscoped weekly window.** A model's own
  weekly window (Fable) does not enter it — one rule per provider, at the cost of putting a
  Fable entry first when only the general window is session-bound; the gate still judges the
  Fable window.
- **Sessions left are counted in real hours, against `budget_threshold`.** Not the
  weekend-weighted clock and not the racing threshold: it is a physical maximum, so it errs
  toward more capacity, which makes a provider session-bound later rather than sooner.
- **`session_week_share` is a mapping of provider to share in `targets.yaml`**, parsed into
  `PaceConfig`. No default: a provider that is not listed is never session-bound — the rule is
  off until the operator writes an estimate, and nothing else branches on the provider name.
- **A weekly window with no remaining weighted time has no required pace.** This covers a reset
  time that has passed and, with `weekend_weight: 0`, a window whose remaining time is all
  weekend — such an entry ranks last (requirement 4) instead of dividing by zero, so on a
  zero-weight weekend auto falls back to the written order for that provider.
- **The mode is one file, `<state_dir>/provider-priority.json`, written only by the console.**
  Same pattern as `execution-overrides/`: temporary file, then rename. The dispatcher, the
  triage sweep and the console each read it when they need an order — no intent file and no
  dispatcher round trip, so a change is visible to the console at once and to routing from the
  next pass; two operators changing it at the same moment is last write wins.
- **Reading never fails.** A missing file, unparseable JSON, a `mode` that is not a string, or a
  provider that the reader's config does not route all read as `auto`. The file is not rewritten
  on a fallback, so a provider that is routed again later brings its stored mode back.
- **Places with no usage reading order with an empty reading.** The board snapshot and the
  dispatcher's status lines must not wait for usage; they apply a fixed mode, and in `auto`
  every entry is unrated, which is the written order. The full board, which has readings, shows
  the true next launch (requirement 11).
- **`gate_entry` is unchanged.** The console header gate and the stall/resume pings keep judging
  the untracked track's first written spec entry — they can say "blocked" while the box
  launches on the other provider, as they already can today with a fallback entry; in exchange
  the pings do not flip between providers as required pace moves.
- **Triage's "skipped — usage gate" note names the first entry in the order tried**, not
  `models.triage[0]` — one more line that depends on the mode, but the note names the provider
  the sweep actually wanted.
- **The "first" provider is decided in the read model.** Fixed mode: that provider. `auto`: the
  session-bound provider if there is one, else the routed provider with the highest required
  pace on its unscoped weekly window, and `anthropic`
  when the highest is shared or no routed provider has one — the chip is a provider-level
  summary, so it can differ from the entry a task gets when a model has its own weekly window.
- **Required pace is shown as `N.N× pace`**, one decimal, on weekly windows only; a weekly
  window with no required pace shows nothing there.
- **The change is one authenticated endpoint, `POST /api/priority`.** It validates against
  `auto` plus the console's routed providers, writes the file, and appends the event
  `priority-mode-set` with the operator's login as actor and `mode=<mode>` as detail — no
  role check beyond the existing operator session, like every other console write.
- **The default changes on deploy.** With no file the box is in `auto`, so multi-provider
  lists stop being "Claude first" the moment this ships; existing tests that assert the written
  order with weekly readings present change with it.
- **`CONTEXT.md` gains "Priority mode" and "Required pace"**, and the `targets.example.yaml`
  comment that says the dispatcher launches the first admitted entry in written order is
  corrected.

## Data model

No database. One file in the dispatcher's state dir, and fields on the console's views.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `provider-priority.json` (written only by the console) | `mode` | string; `auto` or a provider name; anything else, or a provider the reader does not route, reads as `auto` | the box-wide priority mode |
| | `set_by` | operator login; informational | who changed it last (the event log is the history) |
| | `set_at` | ISO 8601 UTC; informational | when |
| | whole file | written atomically (temporary file, then rename), readable by the dispatcher's user; missing or unreadable reads as `auto` | a reader never sees half a file |
| `WindowView` | `required_pace` (float or null) | null for a session window and for a weekly window with no remaining weighted time; otherwise ≥ 0 | the number auto ranks on, shown beside headroom |
| `UsageView` | `priority.mode` | `auto` or a routed provider | the selector's current value |
| | `priority.options` | `auto` first, then the routed providers sorted by name | the selector's choices |
| | `priority.first` | a routed provider; never empty while `anthropic` is routed | the "first" chip |
| `targets.yaml` → `PaceConfig` | `session_week_share` | mapping of provider name to a number above 0 and at most 1; anything else fails the config load; absent means no provider has one | how much of the week one session used up to the threshold spends |
| event log | `priority-mode-set` | `actor` = operator login, `detail` = `mode=<mode>`; written only after the file is | audit of every change |

Unchanged: `TaskState.picks`, `execution-overrides/`, `Window`, `Reading`, `Verdict`, the usage
cache files, `targets.yaml`.

## Identities

None.

## Seams

- `dispatcher.usage.required_pace(w: Window, now: datetime, cfg: PaceConfig) -> float | None` —
  new, pure.
- `dispatcher.usage.session_bound(usage: ProviderUsage, now: datetime, cfg: PaceConfig) ->
  bool` — new, pure.
- `dispatcher.config.load_config(path)` — existing; the new `session_week_share` key.
- `dispatcher.priority` — new module:
  - `AUTO = "auto"`;
  - `load(state_dir, routed: Collection[str]) -> str`;
  - `save(state_dir, mode: str, *, actor: str, now: datetime) -> None`;
  - `order(mode: str, usages: Mapping[str, ProviderUsage], now: datetime, pace: PaceConfig) ->
    Order`, where `Order = Callable[[Sequence[Entry]], tuple[Entry, ...]]`.
- `dispatcher.models.candidates(policy, track, stage, avoid_provider="", order=...)`,
  `resolve(...)` and `triage_entry(policy, admitted, order=...)` — existing, new `order`
  parameter.
- `dispatcher.main.run_pass(cfg, deps)` — existing; the high seam for every launch scenario.
  Usage is placed with the helpers in `tests/usagefakes.py`, the mode with
  `dispatcher.priority.save`, and the launched model is read from the sessions fake and
  `TaskState.picks`.
- `dispatcher.triage.run_sweep(cfg, deps, run)` — existing; the model the sweep runs on and its
  skip note.
- `web.app.create_app(...)` through FastAPI's `TestClient`:
  - `POST /api/priority` with body `{"mode": "<mode>"}` — new; 200 on success, 422 on an
    unknown mode, the existing auth refusal without an operator session;
  - `GET /api/usage` and `GET /api/board` — existing; new `priority` object and
    `required_pace` fields, and the board's next-launch model per task.
- `frontend/src/components/UsagePanel.tsx` rendered in vitest with fixtures — existing.
- `frontend/e2e` Playwright against `fake-api.mjs` — existing; the fake gains
  `POST /api/priority`.
