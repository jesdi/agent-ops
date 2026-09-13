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

**Binding window**: the provider's window with the least headroom.

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
@dataclass(frozen=True)
class Window:
    kind: str                 # "session" | "weekly"
    scope: str | None         # model display name ("Fable") or None = all
    used: float               # 0..1; locked or >=100% → 1.0
    resets_at: datetime       # aware UTC
    length: timedelta         # 5h | 7d — nominal, never from the payload


@dataclass(frozen=True)
class ProviderUsage:
    provider: str             # "anthropic"
    source: str               # "oauth" | "ccusage" | "unavailable"
    fetched_at: float
    windows: tuple[Window, ...]


@dataclass(frozen=True)
class Verdict:
    admitted: bool
    provider: str
    window: Window | None     # the binding window (None = unavailable)
    allowance: float          # for the binding window
    headroom: float           # allowance - used, for the binding window
    reason: str               # "ok" | "over-pace" | "over-threshold" | "unavailable"
```

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

`PaceConfig` is the four knobs below plus the two existing session knobs.

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
4. Headroom per window is `allowance - used`. The binding window is the
   minimum. Admitted iff every considered window has headroom `> 0`.

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
    def fetch(self, state_dir: Path) -> ProviderUsage: ...

ADAPTERS: dict[str, UsageAdapter]   # {"anthropic": AnthropicUsage()}
```

`AnthropicUsage` is today's `fetch_usage` moved: the token ladder (env,
1Password setup token, claude-home store), the OAuth GET, the `ccusage`
fallback (session window only), unavailable last. `_parse_oauth` reads
`limits[]` into windows.

```python
def fetch_all(cfg: Config, now) -> dict[str, ProviderUsage]
```

fetches every provider referenced by the global policy, every target
policy and `triage_model`, and nothing else. A referenced provider with no
adapter or no credentials reports `unavailable`, which fails closed and is
visible; it is never silently absent.

Cache: `state_dir/usage/<provider>.json` holding `fetched_at` and the
serialised `ProviderUsage`, honoured for `MIN_POLL_SECONDS` (180) per file.
`usage-cache.json` is gone; `_dry_run_copy` and the SSE fingerprint follow
the directory.

The proof that the seam works is a `FakeUsage` adapter in tests, registered
under `fake/`, returning any windows a test wants. The Codex adapter is the
first task of the Codex provider work.

### Dispatcher

`budget_ok: bool` becomes `admit: Callable[[str], Verdict]`, built once per
pass:

```python
usages = usage.fetch_all(cfg, now=time.time)
admit = lambda model: usage.admits(usages, model, now, pace_cfg(cfg))
```

The six consumers keep their shape and ask for the model they are about to
spawn: `StartTicket`, `RetryStage`, `SpawnStage` in `_drive_task`,
`_resume_woken`, `_spawn_feedback`, and `_claim_new`, which resolves the
spec-stage model for the candidate before claiming. `run_sweep` asks for
`triage_model` (or the policy default).

`_budget_edge` keys its marker on the verdict for the global policy default,
which is what an idle box would spawn next. Its note names the binding
window and the reason:

```
anthropic week·Fable: 31% used, allowance 29%, headroom −2 pts, resets in 5d 2h
```

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
for the session window. `load_config` validates `timezone` with `zoneinfo`
and `0 <= weekend_weight <= 1`.

### Read model and API

`BudgetView` and `GET /api/budget` are replaced by:

```python
class WindowView(BaseModel):
    kind: str; scope: str | None
    used: float; allowance: float; headroom: float
    minutes_to_reset: float
    severity: str        # "ok" | "close" (headroom <= 0.08) | "blocked"

class ProviderUsageView(BaseModel):
    provider: str; source: str
    windows: list[WindowView]
    would_spawn: bool    # verdict for the policy default model
    binding: WindowView | None

GET /api/usage -> list[ProviderUsageView]
```

The board payload embeds the same list where it embedded `budget`. The SSE
key `budget` is renamed `usage`. `NextClaimView` gains `blocked_by: str`
(the note text above) beside `minutes_to_reset`, and the verdict
`budget-blocked` keeps its name.

Frontend types regenerate from OpenAPI (`pnpm gen:api`), never by hand.

### Console

`BudgetBar` is replaced by `UsagePanel` in the board header, rendering one
group per provider. Per group: a provider label, the will-spawn chip, and one
**headroom bullet** per window, as in the prototype:

```
ANTHROPIC  [will spawn]
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

**`tests/test_config.py`** — the three knobs parse, default, and reject a bad
zone or weight; a `provider/` prefixed model id validates and splits.

**`tests/test_web_read_model.py`** — `ProviderUsageView` severities, binding
window, `blocked_by` text, `would_spawn` for the default model.

**Dispatcher tests** — every `patch_usage` helper returns a fake `admit`;
a Fable-blocked pass still starts a Sonnet ticket; `_budget_edge` notes name
the binding window; `run_sweep` is gated on `triage_model`.

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
