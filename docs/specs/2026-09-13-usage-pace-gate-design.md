# Usage pace gate, per provider and window

## Problem

The dispatcher gates every spawn on one number: utilization of Anthropic's
5-hour session window. The weekly limit is invisible to it, so a box that
runs hard on Saturday can leave the operator without Fable on Tuesday. Fable
has its own weekly counter that the box never reads. And the gate is welded
to one provider, while the next version routes work across several
(OpenAI Codex first, free tiers later) and picks a model per task by its
remaining allowance.

The console shows the same single bar, so the operator cannot tell how much
of the week is left, or whether the box is ahead of pace.

## Scope

In scope:

- read every usage window a provider reports (session, week, week per
  scoped model) through a per-provider adapter;
- a weekly **pace gate**: the box may only spawn while usage is at or under a
  spending schedule that reserves weekday hours over weekend hours;
- a gate that answers per `provider/model`, so an exhausted Fable week blocks
  Fable spawns and nothing else;
- the console header shows remaining and headroom per window, grouped by
  provider (headroom bullets, alternative C of the prototype);
- the seam and data shape a second provider adapter slots into.

Out of scope, deferred by name:

- the Codex usage adapter itself (lands with the Codex provider work, when
  there is an account to test against);
- runtime adapters (how a session runs on a non-Anthropic provider);
- the LLM router that chooses among admitted models by task type;
- usage history, burn-rate charts, sample logs;
- extra-usage credits and spend; the box runs on the subscription.

## Language

**Provider**: a subscription whose usage windows the box spends
(`anthropic`, later `openai`, `nvidia`). Named by the prefix of a model id.

**Window**: one limit a provider reports: a kind (`session`, `weekly`), an
optional model scope (`Fable`), a used fraction, when it resets, and its
nominal length.

**Allowance**: the fraction of a window the box may have consumed by now.
For the session window it is the existing threshold. For a weekly window it
is the weighted time elapsed plus the pace margin.

**Headroom**: allowance minus used. The gate's actual input; positive means
the box may spawn.

**Binding window**: of the windows a model draws on, the one with the least
headroom. The verdict carries its `Reading`.

## Design

### The wire shape, verified

`GET https://api.anthropic.com/api/oauth/usage` (User-Agent
`claude-code/2.0.0`, Bearer OAuth token) returns, among codename keys that
are mostly null and not to be relied on, a `limits` array:

```json
"limits": [
  {"kind": "session",       "group": "session", "percent": 5,
   "severity": "normal", "resets_at": "2026-09-13T11:59:59+00:00",
   "scope": null, "is_active": false},
  {"kind": "weekly_all",    "group": "weekly",  "percent": 13,
   "resets_at": "2026-09-18T12:59:59+00:00", "scope": null},
  {"kind": "weekly_scoped", "group": "weekly",  "percent": 23,
   "resets_at": "2026-09-18T12:59:59+00:00",
   "scope": {"model": {"id": null, "display_name": "Fable"}, "surface": null}}
]
```

Each entry also carries `locked_reason`. The Fable counter exists only as
`weekly_scoped` with a display name and a null id, so scoped windows match
models by display name. The top-level `five_hour` / `seven_day` objects
duplicate the first two entries and are read only when `limits` is absent.

The real payload is committed as `tests/fixtures/anthropic-usage.json` (no
secrets in it) and every parser test reads it.

### Data model — `dispatcher/usage.py` (new, pure)

```python
class WindowKind(StrEnum):
    SESSION = "session"       # .length 5h — nominal, never from the payload
    WEEKLY = "weekly"         # .length 7d


@dataclass(frozen=True)
class Window:
    kind: WindowKind
    scope: str | None         # model display name ("Fable") or None = all
    used: float               # 0..1; locked or >=100% → 1.0
    resets_at: datetime       # aware UTC; length is kind.length


@dataclass(frozen=True)
class ProviderUsage:
    provider: str             # "anthropic"
    source: Source            # "oauth" | "ccusage" | "unavailable"
    fetched_at: float
    windows: tuple[Window, ...]


@dataclass(frozen=True)
class Reading:                # one window judged at a moment
    window: Window
    allowance: float
    headroom: float           # allowance - used


@dataclass(frozen=True)
class Verdict:
    admitted: bool
    provider: str
    binding: Reading | None   # least headroom among the considered readings;
                              # None = unavailable, or no window applies
    reason: Reason            # "ok" | "over-pace" | "over-threshold" | "unavailable"
```

`readings(usage, now, cfg)` returns one `Reading` per window, and none for
an unavailable provider; the gate and the read model both consume them.
`UsageSnapshot` is deleted. Nothing outside `usage.py` computes headroom.

### Allowance

```python
def allowance(w: Window, now: datetime, cfg: PaceConfig) -> float
```

- `kind == "session"`: the existing rule, unchanged — `racing_threshold` when
  `resets_at - now <= racing_minutes`, else `budget_threshold`.
- `kind == "weekly"`: `weighted_elapsed / weighted_length + pace_margin`,
  capped at 1.0.

Weighted time is integrated over local calendar days: each day's overlap
with the window counts at `1.0`, or at `weekend_weight` for Saturday and
Sunday in `cfg.timezone`, so DST days weigh their true 23 or 25 hours. The window
start is `resets_at - length`. `seven_day_breakdown.window_started_at` is
never read: if Anthropic grants a mid-week reset, utilization drops and the
schedule stays put, which is the behaviour wanted. Should `resets_at` itself
ever move on such a reset, the fix is to persist the first-seen `resets_at`
per window until it passes; that is not built until observed.

`PaceConfig` holds the three pace knobs below plus the three session knobs;
`load_config` builds it once as `Config.pace`, which the dispatcher, the
triage sweep and the web read model all pass to `admits`.

### The gate

```python
def admits(usages: Mapping[str, ProviderUsage], model_id: str,
           now: datetime, cfg: PaceConfig) -> Verdict
```

1. Split `model_id` into `(provider, bare)`; a bare id means `anthropic`.
2. The provider's usage is missing or `source == "unavailable"` → denied,
   `reason="unavailable"` (fail closed, as today).
3. Consider every window whose scope is `None`, plus every scoped window
   whose display name is a case-insensitive substring of the bare id
   (`"Fable"` matches `claude-fable-5-1`; `"Opus"` would match
   `claude-opus-4-8`).
4. Each considered window yields a `Reading` with headroom
   `allowance - used`; the verdict's `binding` is the one with the least.
   Admitted iff every considered reading has headroom `> 0`. No considered
   window at all admits, with `binding=None`.

A window the adapter could not report is simply absent and passes; only a
whole provider fails closed. `machine.py` and `loops.py` do not change: a
denied verdict is still "not launched this pass, nothing mutated".

### Model ids carry the provider

`models.py` gains `split_model_id(model_id) -> tuple[str, str]`. Validation
accepts one optional `provider/` prefix; the bare form still validates and
means `anthropic/`, so no `targets.yaml` changes. `_model_for` returns the
full id; `stage.json`, the event log and the console display it in full; the
session spawn passes the bare id to the CLI. The same model can appear under
two providers (`anthropic/claude-sonnet-4-6`, `nvidia/claude-sonnet-4-6`)
and they spend different windows.

### Adapters — `dispatcher/usage_providers.py`

```python
class UsageAdapter(Protocol):
    name: str
    def fetch(self, state_dir: Path, *,
              now: Callable[[], float] = time.time) -> ProviderUsage: ...

ADAPTERS: dict[str, UsageAdapter]   # {"anthropic": AnthropicUsage()}
```

`AnthropicUsage` is today's `fetch_usage` moved: the token ladder (env,
1Password setup token, claude-home store), the OAuth GET, the `ccusage`
fallback (session window only), unavailable last. `parse_anthropic` reads
`limits[]` into windows; an entry it cannot read (not an object, unknown
kind, no reset, no percent) is skipped, and a scope of unexpected shape
reads as unscoped, so the window counts against every model.

Every unreadable reading fails closed as `unavailable`. An OAuth response
that parses to no windows counts like a rejected token (next token, then
`ccusage`, then `unavailable`), because a reading with no windows would
admit every model. `fetch_provider` catches any exception an adapter
raises and reports that provider `unavailable`, caching nothing, so one
broken adapter never crashes the pass or `/api/board`.

```python
def fetch_all(cfg: Config, *, now=time.time,
              adapters=ADAPTERS) -> dict[str, ProviderUsage]
```

fetches every provider referenced by the global policy, every target
policy and `triage_model`, and nothing else. A referenced provider with no
adapter or no credentials reports `unavailable`, which fails closed and is
visible; it is never silently absent.

Cache: `state_dir/usage/<provider>.json` holding `fetched_at` and the
serialised `ProviderUsage`, honoured for `MIN_POLL_SECONDS` (180) per file.
Only readable results are cached, so an outage never masks a recovery. The
web and dispatcher units both write it, possibly as different users: each
write goes to a sibling temp file, set to mode 0644, and is renamed over.
A cache file that cannot be read or parsed, a negative age (clock stepped
back), or a cached reading that is not `unavailable` yet has no windows is
a miss and refetches. `usage-cache.json` is gone; the SSE fingerprint
follows the directory.

The proof that the seam works is a `FakeUsage` adapter in tests, registered
under `fake/`, returning any windows a test wants. The Codex adapter is the
first task of the Codex provider work.

### Dispatcher

`budget_ok: bool` becomes `admit: Admit` (`Callable[[str], Verdict]`),
built once per pass:

```python
usages = fetch_all(cfg)
now = datetime.now(timezone.utc)
admit: Admit = lambda model: admits(usages, model, now, cfg.pace)
```

The six consumers keep their shape and ask for the model they are about to
spawn, resolved once as a `Launch(stage, model)` so the gate asks about
exactly the model the spawner then launches: `StartTicket`, `RetryStage`,
`SpawnStage` in `_drive_task`, `_resume_woken`, `_spawn_feedback`, and
`_claim_new`, whose `_claimable` resolves each candidate's spec-stage
`Launch` before claiming; a denied candidate leaves the next one eligible,
since its model may differ. `run_sweep` asks for `triage_model` (or the
policy default).

`_budget_edge` keys its marker on the verdict for the global policy default,
which is what an idle box would spawn next. Its note (`verdict_note`) names
the binding window and its numbers:

```
anthropic week·Fable: 31% used, allowance 29%, headroom −2 pts, resets in 5d 2h
```

The pings use resume hysteresis. The stall ping fires on the first denied
pass; the resume ping waits until the default is admitted with headroom
of at least `RESUME_HEADROOM` (2 pts), since the weekly allowance grows
continuously and a box running at pace would otherwise ping a stall/resume
pair at every zero crossing. An `unavailable` verdict sends neither: there
is no window to wait out, and `_auth_dark_edge` owns that case.

`budget_stall` copy changes from "usage window exhausted; stalled until
reset" to "usage gate closed; resumes when headroom returns", since a pace
block clears as time passes, not at a reset. `_auth_dark_edge` fires when
every fetched provider is unavailable.

### Config

`targets.yaml`, all optional, defaults shown:

```yaml
pace_margin: 0.10       # how far ahead of the weighted schedule the box may run
weekend_weight: 0.5     # a weekend hour counts this much of a weekday hour
timezone: UTC           # IANA zone that defines Saturday 00:00 – Monday 00:00
```

`budget_threshold`, `racing_minutes`, `racing_threshold` keep their meaning
for the session window. `load_config` validates `timezone` with `zoneinfo`,
`0 <= weekend_weight <= 1` and `0 <= pace_margin < 1`, and gathers all six
knobs into `Config.pace: PaceConfig`.

### Read model and API

`BudgetView` and `GET /api/budget` are replaced by:

```python
class WindowView(BaseModel):
    kind: WindowKind; scope: str | None
    used: float; allowance: float; headroom: float
    minutes_to_reset: float
    severity: Severity   # "ok" | "close" (headroom <= 0.08) | "blocked"

class ProviderUsageView(BaseModel):
    provider: str; source: Source
    windows: list[WindowView]

class GateView(BaseModel):   # the verdict for the policy default model
    model: str; provider: str
    admitted: bool
    note: str                # verdict_note: the binding window and its numbers
    minutes_to_reset: float  # of the binding window; 0 when there is none
    binding: WindowView | None

class UsageView(BaseModel):
    providers: list[ProviderUsageView]   # sorted by provider name
    gate: GateView

GET /api/usage -> UsageView
```

There is one gate, not a verdict per provider: what an idle box would spawn
next, and the verdict the stall/resume pings key on. The board payload does
not embed usage; `/api/board` builds the same gate and `next_claim`
consumes it (`gate: GateView`): a gate that does not admit forecasts
`budget-blocked` with the gate's `minutes_to_reset` and
`blocked_by = gate.note`. The forecast stays a partial mirror, since the
dispatcher gates each candidate on its own spec model. The SSE key `budget`
is renamed `usage`. `NextClaimView` gains `blocked_by: str` beside
`minutes_to_reset`, and the verdict `budget-blocked` keeps its name.

Frontend types regenerate from OpenAPI (`pnpm gen:api`), never by hand.

### Console

`BudgetBar` is replaced by `UsagePanel` in the board header, rendering one
group per provider. Per group: a provider label and one **headroom bullet**
per window, as in the prototype. The console shows one spawn chip, on the
group of the gate's provider: `will spawn <model>` or `will not spawn
<model>` for the policy default, titled with `gate.note`.

```
ANTHROPIC  [will spawn claude-sonnet-4-6]
Session · 5h   [▮▮░░░░░░░░░░░░░░░░░░│░░░]  95% left
1h 29m                          cap 80%
Week · all     [▮▮▮░░░░░░│░░░░░░░░░░░░░░]  87% left
5d 2h              headroom 15.9 pts
Week · Fable   [▮▮▮▮▮▮▮░░│░░░░░░░░░░░░░░]  77% left
5d 2h              headroom 5.9 pts
```

- the hatched band runs from 0 to the allowance: what the box may have
  spent by now; the head line closes it;
- the fill is used; its colour is the window's severity (emerald / amber /
  red), never the accent;
- remaining is text to the right of the track; headroom (or `cap` for the
  session window) is the label under the head line;
- rows carry `role="progressbar"` with `aria-valuetext` spelling used,
  allowance and remaining, matching `CapacityMeter`;
- `source == "unavailable"` renders the existing amber "usage unknown"
  box for that provider only; a window absent from a `ccusage` reading is
  simply not drawn and the group footer reads `via ccusage · weekly unknown`.

`NextClaimLine` renders `blocked_by` for `budget-blocked` instead of the
reset countdown. Prototype: alternative C of the published page
`Usage Pace Alternatives`.

### CONTEXT.md

Add Provider, Window, Allowance, Headroom and Binding window to Language.
Update the loop-policy section's "Future" paragraph: usage collectors and
per-model admission now exist in `usage.py`; the router and runtime adapters
remain deferred. The third question ("which suitable model has allowance")
is now answered by `admits`, and it still never touches loop accounting.

## Edge cases

- **Week's first day.** Saturday 13:00 CEST with 13% used, window Fri
  14:59 → Fri 14:59 CEST: weighted elapsed 10.8%, allowance 20.8%, admitted. Without the margin every week would
  start blocked; the margin is the knob if it still feels tight.
- **Weekend.** Weighted hours advance at half rate, so the allowance grows
  slowly Saturday–Sunday and the saved share flows to Monday–Friday. This is
  deliberate: the operator cannot attend sessions on weekends.
- **Week's last hours.** Allowance approaches `1 + pace_margin`, capped at 1,
  so the rule relaxes on its own; no weekly reset-racing.
- **Scoped window for a model the box never spawns.** Considered only when
  the id matches, so a saturated Fable week leaves Sonnet stages running.
- **Mid-week goodwill reset.** Used drops, schedule unchanged, headroom
  grows. If `resets_at` moves, see Allowance.
- **Locked window** (`locked_reason` set) or `percent >= 100` → `used = 1.0`,
  headroom negative, blocked, red.
- **Session window absent** (ccusage down, OAuth up): passes; the console
  labels it unknown.
- **Provider referenced but no adapter** (`nvidia/…` before its adapter):
  `unavailable`, blocked, visible in the header; the operator sees why.
- **Capacity lowered / triage sweep.** Unchanged; the gate composes with
  capacity exactly as `budget_ok` did.
- **Timezone DST.** Weighting is computed hour by hour in local time via
  `zoneinfo`, so the 23- and 25-hour days weigh what they are.

## Testing

**`tests/test_usage.py`** (pure)
- parser: the committed fixture yields three windows with the right kinds,
  scopes, used fractions, resets; `limits` absent → falls back to
  `five_hour`/`seven_day`; locked → `used == 1.0`.
- allowance: table over (now, resets_at, weekend_weight, timezone), window
  resets Fri 2026-09-18 12:59:59 UTC, zone Europe/Madrid: Saturday 13:00 CEST
  → 10.8% + margin; Sunday 12:30 → 18.9% + margin; Monday 13:00 → 31.9% +
  margin; Wednesday 13:00 → 65.3% + margin; Friday 12:59 → 1.0;
  `weekend_weight=1` reproduces linear time; DST week sums to the right total.
- admits: Fable over pace blocks `anthropic/claude-fable-5-1` and admits
  `claude-sonnet-4-6`; weekly_all over pace blocks both; unavailable
  provider blocks; a `fake/` model spends the fake adapter's windows, not
  Anthropic's; absent session window passes.
- session rule regression: the existing `test_budget_policy` truth table
  reproduced through `admits` on a session-only usage.

**`tests/test_usage_providers.py`**
- token ladder and cache floor moved from `test_budget_fetch`, keyed per
  provider file; `fetch_all` fetches only referenced providers; missing
  adapter → unavailable.
- an empty OAuth reading, a malformed payload and a raising adapter all
  report `unavailable`; the cache is written aside at mode 0644; an
  unreadable or empty cached reading refetches.

**`tests/test_config.py`** — the three knobs parse, default, and reject a bad
zone or weight; a `provider/` prefixed model id validates and splits.

**`tests/test_web_read_model.py`** — `WindowView` severities; the gate's
admitted, binding window and note for the default model; `next_claim` takes
`blocked_by` and `minutes_to_reset` from the gate.

**Dispatcher tests** — every `patch_usage` helper returns a fake `admit`;
a Fable-blocked pass still starts a Sonnet ticket; `_budget_edge` notes name
the binding window, resume waits for 2 pts of headroom, and an unavailable
verdict pings nothing; `run_sweep` is gated on `triage_model`.

**Frontend** — `UsagePanel.test.tsx`: bullet geometry from `allowance` and
`used`, severity classes asserted through accessible text, unavailable
provider box, ccusage weekly-unknown footer; `NextClaimLine` shows
`blocked_by`; `BoardPage` fixture carries two providers to prove grouping.

## Mechanical follow-through

- `tests/fixtures/anthropic-usage.json` — the committed payload.
- `frontend/src/test/fixtures.ts` — usage fixtures replace budget ones.
- `frontend/src/lib/api-types.ts` — regenerated.
- `targets.example.yaml` — the three knobs with comments; one example rule
  using a `provider/` prefixed id.
- `README.md` — "Budget-aware spawning" paragraph describes the weekly pace
  and the per-model verdict; the "Stalled on budget" column keeps its name.
- `telegram/templates.py` — `budget_stall` / `budget_resume` copy.
