# Provider priority: choose which subscription the box spends first — Spec

Terms:

- The **priority mode** is one box-wide value: `auto`, or the name of a provider (`anthropic`,
  `openai`), read as "that provider first".
- A **routed provider** is a provider that some entry in the box's model policies names.
- **Remaining quota** of a weekly window is 1 minus its used fraction, clamped at 0.
- **Remaining time** of a weekly window is the share of the window still ahead of now, measured
  on the usage gate's weekend-weighted clock (`weekend_weight`, `timezone`). There is no floor:
  the closer the reset, the smaller the remaining time.
- The **required pace** of a weekly window is remaining quota divided by remaining time. 1.0×
  means spending the rest evenly finishes the window exactly at its reset; 3.0× means the quota
  left is three times what the time left would use at an even rate.
- The required pace of an **entry** is the lowest required pace among the weekly windows its
  model draws on: the provider's unscoped weekly window plus any scoped weekly window whose
  name matches the model (the same rule the gate uses).
- The **session week share** of a provider is a `targets.yaml` value: the share of the weekly
  quota that one 5-hour session spends when it is used up to the session threshold. A provider
  without one has no session limit for this spec.
- The **spendable maximum** of a provider with a session week share is the most of its weekly
  quota it can still spend before its unscoped weekly window resets: the session week share
  times the sessions left. The open session counts for the part of the threshold it has not
  used (threshold 80%, 40% used: half a session; at or over the threshold: none). After the
  open session resets, every started 5 hours of real time up to the weekly reset counts as one
  session. With no session window reported, every started 5 hours from now counts as one.
- A provider is **session-bound** when the remaining quota of its unscoped weekly window is at
  least its spendable maximum: even using every session left to the threshold, it cannot spend
  more than it has.

Unless a scenario says otherwise, `weekend_weight` is 1.0, so a weekly window is 168 equal
hours, and the stage list is `[claude-sonnet-5-5, openai/gpt-6-luna@high]`.

## Requirements

1. **Auto is the default.** With no stored mode, or a stored mode that cannot be read or that
   names a provider no longer routed, the box behaves as in `auto`.
2. **Auto orders a list by required pace.** In `auto`, the entries of a list are tried in
   descending order of required pace. Entries with equal required pace keep their
   `targets.yaml` order. Requirement 12 comes before this order.
3. **Remaining time uses the gate's clock and has no floor.** Remaining time is weekend-weighted
   exactly as the gate's allowance is. A window close to its reset is ranked on its true
   remaining time, however small.
4. **An entry without a weekly reading ranks last.** In `auto`, an entry whose provider's usage
   is unavailable, whose model draws on no weekly window, or whose weekly window's reset time
   has already passed (no remaining time to divide by), is tried after every entry that has a
   required pace, in `targets.yaml` order among themselves.
5. **A fixed mode moves one provider to the front.** In a provider-first mode, that provider's
   entries are tried first and the others after, each group in `targets.yaml` order. A list
   with no entry of that provider is unchanged.
6. **The gate still decides.** In every mode the box launches the first entry in the resulting
   order that the usage gate admits, and waits when none is admitted. The mode changes no
   verdict: the session threshold (80% of the 5-hour window by default), the weekly allowance
   and the headroom are computed exactly as today, and required pace is never an input to
   them.
7. **Review independence wins.** For a review stage, the entries of the provider that ran
   implement are tried last, after the mode's ordering has been applied to the rest.
8. **The mode covers every routed list.** It orders the stage lists of every track, of every
   target's policy, and the `models.triage` list. (Amended by `specs/pinned-tracks`: the stage
   lists of a pinned track are not reordered.)
9. **The mode only affects launches that have no pick or override.** A stage that already has a
   pick keeps it. A one-shot operator override launches the model it names, whatever the mode.
10. **The operator sets the mode in the console.** The console offers `auto` and one option per
    routed provider. A change is stored durably (it survives a dispatcher or console restart),
    applies from the next dispatcher pass, and records an event naming the operator and the new
    mode. A value that is neither `auto` nor a routed provider is refused and changes nothing.
11. **The console shows what the mode is doing.** The usage panel shows the current mode, marks
    exactly one routed provider as first (the fixed provider, or in `auto` the provider with
    the highest required pace on its unscoped weekly window; `anthropic` when the highest is
    shared or no routed provider has one), and shows the required pace on
    each weekly window, next to the allowance and headroom it shows today, which do not change.
    The model the console names as a task's next launch is the one the
    dispatcher would launch under the current mode.
12. **In auto, a session-bound provider goes first.** In `auto`, the entries of a session-bound
    provider are tried before all others; inside that group and inside the rest, requirement
    2's order applies. A fixed mode ignores this rule. A provider with no session week share,
    with no unscoped weekly window, or whose usage is unavailable is never session-bound. The
    session week share must be above 0 and at most 1, or the config fails to load. The console
    marks a session-bound provider as first in `auto`.

## Scenarios

### Scenario: no stored mode behaves as auto

- **Given** no priority mode has ever been set, anthropic's weekly window is 10% used and
  resets in 4 days, and openai's weekly window is 20% used and resets in 1 day
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`, and the console shows the mode as `auto`

### Scenario: an unreadable stored mode behaves as auto

- **Given** the stored mode is not valid (truncated file), with the same usage as above
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`, and the console shows the mode as `auto`

### Scenario: a stored provider that is no longer routed behaves as auto

- **Given** the stored mode is `nvidia` and no entry in any policy names `nvidia`, with the
  same usage as above
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`, and the console shows the mode as `auto`

### Scenario: auto puts the nearer deadline first

- **Given** mode `auto`; anthropic's weekly window is 10% used and resets in 4 days (required
  pace 0.90 / 0.571 = 1.6×); openai's weekly window is 20% used and resets in 1 day (required
  pace 0.80 / 0.143 = 5.6×)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`

### Scenario: auto ranks by ratio, not by difference

- **Given** mode `auto`; anthropic's weekly window is 20% used and resets in 84 h (remaining
  quota 80%, remaining time 50%, required pace 1.6×); openai's weekly window is 70% used and
  resets in 16.8 h (remaining quota 30%, remaining time 10%, required pace 3.0×)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`, although anthropic has the larger gap between
  remaining quota and remaining time

### Scenario: equal required pace keeps the written order

- **Given** mode `auto`; both weekly windows are 50% used and reset in 84 h (1.0× each)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `claude-sonnet-5-5`

### Scenario: remaining time is weekend-weighted

- **Given** mode `auto`, `weekend_weight: 0.5`, `timezone: UTC`, now Saturday 2026-10-03 00:00
  UTC; anthropic's weekly window is 60% used and resets Monday 2026-10-05 00:00 UTC (24 of 144
  weighted hours remain, 16.7%, required pace 2.4×); openai's weekly window is 0% used and
  resets Wednesday 2026-10-07 00:00 UTC (72 of 144 weighted hours remain, 50%, required pace
  2.0×)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `claude-sonnet-5-5` (on an unweighted clock openai would rank first,
  1.75× against 1.4×)

### Scenario: a window about to reset ranks on its true remaining time

- **Given** mode `auto`; openai's weekly window is 95% used and resets in 1 h (remaining quota
  5%, remaining time 0.6%, required pace 8.4×); anthropic's weekly window is 40% used and
  resets in 33.6 h (remaining time 20%, required pace 3.0×)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`

### Scenario: a weekly window past its reset time ranks last

- **Given** mode `auto`; openai's last reading has a weekly window 50% used whose reset time
  was 5 minutes ago; anthropic's weekly window is 90% used and resets in 84 h (0.2×); the
  stage list is `[openai/gpt-6-luna@high, claude-sonnet-5-5]`
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `claude-sonnet-5-5`

### Scenario: a model-scoped weekly window lowers its entry's rank

- **Given** mode `auto`; anthropic reports an unscoped weekly window 20% used and a weekly
  window scoped `Fable` 90% used, both resetting in 84 h; openai's weekly window is 50% used
  and resets in 84 h (1.0×); the stage list is `[claude-fable-5-1, openai/gpt-6-sol]`
- **When** a task enters that stage and the gate admits both entries
- **Then** it launches `openai/gpt-6-sol` (the Fable entry's required pace is the lower of
  1.6× and 0.2×)

### Scenario: the scoped window does not affect another model of the same provider

- **Given** the same usage, and the stage list `[claude-opus-5-5, openai/gpt-6-sol]`
- **When** a task enters that stage and the gate admits both entries
- **Then** it launches `claude-opus-5-5` (1.6× against 1.0×)

### Scenario: an unavailable provider ranks last in auto

- **Given** mode `auto`; openai's usage is unavailable; anthropic's weekly window is 90% used
  and resets in 84 h (0.2×); the stage list is `[openai/gpt-6-luna@high, claude-sonnet-5-5]`
- **When** a task enters `implement`
- **Then** the order tried is `claude-sonnet-5-5`, then `openai/gpt-6-luna@high`, and it
  launches `claude-sonnet-5-5` if the gate admits it

### Scenario: a provider that reports no weekly window ranks last in auto

- **Given** mode `auto`; openai reports only a session window, 10% used; anthropic's weekly
  window is 90% used and resets in 84 h; the stage list is
  `[openai/gpt-6-luna@high, claude-sonnet-5-5]`
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `claude-sonnet-5-5`

### Scenario: a session-bound provider goes first at the boundary

- **Given** mode `auto`, `session_week_share: {anthropic: 0.05}`, session threshold 80%;
  anthropic's session window is 0% used and resets in 5 h; its weekly window is 70% used and
  resets in 30 h (1 open session plus 5 more, spendable maximum 6 × 5% = 30%, remaining quota
  30%, required pace 1.7×); openai's weekly window is 20% used and resets in 1 day (5.6×)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `claude-sonnet-5-5`

### Scenario: one point under the spendable maximum follows required pace

- **Given** the same, but anthropic's weekly window is 71% used (remaining quota 29%, under the
  spendable maximum of 30%)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`

### Scenario: a partly used open session counts for what is left of it

- **Given** mode `auto`, `session_week_share: {anthropic: 0.05}`, session threshold 80%;
  anthropic's session window is 40% used and resets in 5 h; its weekly window is 72% used and
  resets in 30 h (half a session plus 5 more, spendable maximum 27.5%, remaining quota 28%);
  openai's weekly window is 20% used and resets in 1 day
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `claude-sonnet-5-5`

### Scenario: with no session window reported, every started 5 hours counts

- **Given** mode `auto`, `session_week_share: {anthropic: 0.05}`; anthropic reports no session
  window; its weekly window is 70% used and resets in 28 h (6 started 5-hour periods, spendable
  maximum 30%); openai's weekly window is 20% used and resets in 1 day
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `claude-sonnet-5-5`

### Scenario: a session-bound provider at its session cap is still denied

- **Given** mode `auto`, `session_week_share: {anthropic: 0.05}`, session threshold 80%;
  anthropic's session window is 80% used and resets in 2 h; its weekly window is 40% used and
  resets in 30 h (no open-session capacity, 6 more sessions, spendable maximum 30%, remaining
  quota 60%); the gate admits `openai/gpt-6-luna@high`
- **When** a task enters `implement`
- **Then** the gate denies `claude-sonnet-5-5` and the task launches `openai/gpt-6-luna@high`

### Scenario: a fixed mode ignores the session-bound rule

- **Given** mode `openai`, and anthropic session-bound as in the boundary scenario
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`

### Scenario: no session week share means no session-bound rule

- **Given** mode `auto`, no `session_week_share` in `targets.yaml`, and the usage of the
  boundary scenario
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`

### Scenario: review still avoids a session-bound implement provider

- **Given** mode `auto`, anthropic session-bound as in the boundary scenario; a task whose
  `implement` pick is `claude-sonnet-5-5`; the review list is
  `[claude-opus-5-5, openai/gpt-6-sol]`
- **When** the task enters `review` and the gate admits both entries
- **Then** it launches `openai/gpt-6-sol`

### Scenario: a session week share out of range fails the config load

- **Given** `session_week_share: {anthropic: 0}` (or `1.5`) in `targets.yaml`
- **When** the config is loaded
- **Then** loading fails with an error that names `session_week_share`

### Scenario: the console marks a session-bound provider first

- **Given** mode `auto`, anthropic session-bound as in the boundary scenario (required pace
  1.7× against openai's 5.6×)
- **When** the console loads the usage panel
- **Then** anthropic, and only anthropic, is marked first

### Scenario: a fixed mode puts its provider first

- **Given** mode `openai`; anthropic's weekly window is 10% used and resets in 1 day (6.3×);
  openai's weekly window is 50% used and resets in 84 h (1.0×)
- **When** a task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-luna@high`

### Scenario: a fixed mode whose provider has no usage reading uses the other provider

- **Given** mode `openai`; openai's usage is unavailable (the box is logged out of Codex);
  anthropic's weekly window is 40% used and resets in 84 h
- **When** a task enters `implement`
- **Then** the gate denies `openai/gpt-6-luna@high` as unavailable and the task launches
  `claude-sonnet-5-5`

### Scenario: a fixed mode keeps the written order inside each group

- **Given** mode `openai` and the stage list
  `[claude-opus-5-5, openai/gpt-6-sol, claude-sonnet-5-5, openai/gpt-6-luna@high]`
- **When** the box orders that list
- **Then** the order tried is `openai/gpt-6-sol`, `openai/gpt-6-luna@high`, `claude-opus-5-5`,
  `claude-sonnet-5-5`

### Scenario: a fixed mode leaves a list without that provider unchanged

- **Given** mode `openai` and the stage list `[claude-opus-5-5, claude-sonnet-5-5]`
- **When** a task enters that stage and the gate admits both entries
- **Then** it launches `claude-opus-5-5`

### Scenario: a denied priority entry falls to the next one

- **Given** mode `openai`; the gate denies `openai/gpt-6-luna@high` (weekly window over its
  pace) and admits `claude-sonnet-5-5`
- **When** a task enters `implement`
- **Then** it launches `claude-sonnet-5-5`, and the gate's verdict for
  `openai/gpt-6-luna@high` is the same as it would be in `auto`

### Scenario: the first entry in auto is denied by its session window

- **Given** mode `auto`; openai ranks first on required pace (5.6× against 1.6×) but its
  session window is over the session threshold, so the gate denies it; the gate admits
  `claude-sonnet-5-5`
- **When** a task enters `implement`
- **Then** it launches `claude-sonnet-5-5`

### Scenario: anthropic first on required pace, session window at the 80% cap

- **Given** mode `auto`, session threshold 80%; anthropic ranks first on required pace (5.6×
  against openai's 1.6×); anthropic's 5-hour session window is 80% used and resets in 2 h; the
  gate admits `openai/gpt-6-luna@high`
- **When** a task enters `implement`
- **Then** the gate denies `claude-sonnet-5-5` and the task launches `openai/gpt-6-luna@high`

### Scenario: anthropic first on required pace, session window under the cap

- **Given** the same, but anthropic's session window is 79% used
- **When** a task enters `implement`
- **Then** it launches `claude-sonnet-5-5`

### Scenario: the mode does not change the gate's numbers

- **Given** anthropic's weekly window is 60% used and its session window 50% used
- **When** the mode is changed from `auto` to `openai` and the console reloads the usage panel
- **Then** anthropic's session cap, weekly allowance and weekly headroom are the same values as
  before the change

### Scenario: nothing admitted still waits

- **Given** mode `openai` and the gate denies both entries
- **When** a task enters `implement`
- **Then** nothing is launched, the task has no pick for `implement`, and it is retried on a
  later pass

### Scenario: review avoids the implement provider under a fixed mode

- **Given** mode `openai`; a task whose `implement` pick is `openai/gpt-6-luna@high`; the
  review list is `[claude-opus-5-5, openai/gpt-6-sol]`
- **When** the task enters `review` and the gate admits both entries
- **Then** it launches `claude-opus-5-5`

### Scenario: review avoids the implement provider in auto

- **Given** mode `auto`; openai ranks first on required pace (5.6× against 1.6×); a task whose
  `implement` pick is `openai/gpt-6-luna@high`; the review list is
  `[claude-opus-5-5, openai/gpt-6-sol]`
- **When** the task enters `review` and the gate admits both entries
- **Then** it launches `claude-opus-5-5`

### Scenario: review follows the mode when implement ran on the other provider

- **Given** mode `openai`; a task whose `implement` pick is `claude-sonnet-5-5`; the review
  list is `[claude-opus-5-5, openai/gpt-6-sol]`
- **When** the task enters `review` and the gate admits both entries
- **Then** it launches `openai/gpt-6-sol`

### Scenario: the mode orders the triage list

- **Given** mode `openai` and `models.triage: [claude-sonnet-5-5, openai/gpt-6-luna]`
- **When** triage runs and the gate admits both entries
- **Then** the triage session runs on `openai/gpt-6-luna`

### Scenario: the mode orders a target's own policy

- **Given** mode `openai` and a target with its own `models:` block whose `standard` track has
  `implement: [claude-opus-5-5, openai/gpt-6-sol]`
- **When** a `standard` task of that target enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-6-sol`

### Scenario: a stage with a pick is not rerouted

- **Given** mode `auto`, and a task whose `implement` pick is `claude-sonnet-5-5` with two
  tickets left
- **When** the operator sets the mode to `openai` and the next implement session of that task
  starts
- **Then** it runs on `claude-sonnet-5-5`

### Scenario: the next stage follows the new mode

- **Given** the same task, mode now `openai`, its `implement` pick `claude-sonnet-5-5`, and the
  review list `[claude-opus-5-5, openai/gpt-6-sol]`
- **When** the task enters `review` and the gate admits both entries
- **Then** it launches `openai/gpt-6-sol`

### Scenario: a one-shot override beats the mode

- **Given** mode `openai`, and the operator has armed a one-shot run of a task with
  `claude-opus-5-5`
- **When** the task's next stage starts and the gate admits `claude-opus-5-5`
- **Then** it launches `claude-opus-5-5`

### Scenario: the operator sets a fixed mode

- **Given** mode `auto`, routed providers `anthropic` and `openai`, and operator `jesdi`
  signed in to the console
- **When** they choose `openai` in the usage panel
- **Then** the console shows the mode as `openai`, an event records that `jesdi` set the
  priority mode to `openai`, and the next dispatcher pass orders lists with openai first

### Scenario: the mode survives a restart

- **Given** the mode was set to `openai`
- **When** the dispatcher and the console are both restarted
- **Then** the console shows the mode as `openai`, and the next pass orders lists with openai
  first

### Scenario: the selector offers only auto and routed providers

- **Given** policies whose entries name only `anthropic` and `openai`
- **When** the console loads the usage panel
- **Then** the selector offers exactly `auto`, `anthropic` and `openai`

### Scenario: an unknown mode is refused

- **Given** mode `auto` and routed providers `anthropic` and `openai`
- **When** a signed-in operator submits the mode `nvidia`
- **Then** the request is refused, the mode is still `auto`, and no event is recorded

### Scenario: an unauthenticated change is refused

- **Given** mode `auto`
- **When** a request with no operator session submits the mode `openai`
- **Then** the request is refused and the mode is still `auto`

### Scenario: the console marks the leading provider in auto

- **Given** mode `auto`; anthropic's unscoped weekly window has required pace 1.6×, openai's
  5.6×
- **When** the console loads the usage panel
- **Then** openai, and only openai, is marked first, and the weekly windows show `1.6× pace`
  and `5.6× pace`

### Scenario: equal required pace marks anthropic first

- **Given** mode `auto`; both unscoped weekly windows have required pace 1.0×
- **When** the console loads the usage panel
- **Then** anthropic, and only anthropic, is marked first

### Scenario: no required pace anywhere marks anthropic first

- **Given** mode `auto`; anthropic and openai both report usage as unavailable
- **When** the console loads the usage panel
- **Then** anthropic, and only anthropic, is marked first

### Scenario: the console marks the fixed provider

- **Given** mode `anthropic`; anthropic's unscoped weekly window has required pace 1.6×,
  openai's 5.6×
- **When** the console loads the usage panel
- **Then** anthropic, and only anthropic, is marked first

### Scenario: the console's next launch matches the dispatcher

- **Given** mode `openai` and a queued task with no pick whose next stage list is
  `[claude-sonnet-5-5, openai/gpt-6-luna@high]`, both admitted
- **When** the console shows that task's next launch
- **Then** it names `openai/gpt-6-luna@high`, the model the dispatcher then launches

## Out of scope

- Exclusive routing, and any change to the usage gate's verdicts.
- A "written order" mode, a per-track opt-out, and a per-target mode.
- Automatic expiry of a fixed mode.
- Ranking from burn-rate history.
- Setting the mode from `targets.yaml` or from Telegram.
- A database for box configuration.
