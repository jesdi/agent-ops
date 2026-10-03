# Provider priority: choose which subscription the box spends first — Tasks

Each task links to the goal in proposal.md: the box orders every stage's entries by a provider
priority mode the operator picks in the console — auto by default, or one provider first — while
the usage gate keeps deciding what may run. Work the frontier (tasks whose blockers are done),
commit per task with its ID, and finish every task with `make test` green; tasks that touch
`frontend/` also finish with `pnpm gen:api` leaving no diff, `pnpm build`, `pnpm test` and
`pnpm test:e2e` green there. Tests come first in every task.

- [ ] **T1** — Auto orders launches by required pace. Add required pace for a weekly window
  (remaining quota over remaining weekend-weighted time; none for a session window or a window
  with no remaining weighted time) and the required pace of an entry (lowest among the weekly
  windows its model draws on). With no stored mode, a stage with no pick launches the admitted
  entry with the highest required pace; equal values keep the written order; entries with no
  required pace (provider unavailable, no weekly window, reset passed) go last in written order.
  The gate is untouched: an entry ranked first and denied falls to the next, and nothing
  admitted still waits. Review still puts implement's provider last, a stage with a pick keeps
  it, and a one-shot override launches its model. Add **Required pace** to `CONTEXT.md` and
  correct the routing comment in `targets.example.yaml`. Covers "no stored mode behaves as
  auto" (dispatcher half), "auto puts the nearer deadline first", "auto ranks by ratio, not by
  difference", "equal required pace keeps the written order", "remaining time is
  weekend-weighted", "a window about to reset ranks on its true remaining time", "a weekly
  window past its reset time ranks last", both model-scoped window scenarios, "an unavailable
  provider ranks last in auto", "a provider that reports no weekly window ranks last in auto",
  "the first entry in auto is denied by its session window", both "anthropic first on required
  pace, session window…" scenarios, "review avoids the implement provider in auto" and "a stage
  with a pick is not rerouted" (auto half). _Goal: by default the box spends first the
  subscription whose week most needs it._
  Seam: `dispatcher.main.run_pass(cfg, deps)` for launches;
  `dispatcher.usage.required_pace(w, now, cfg)` for the arithmetic cases (weekend weighting, no
  remaining time). Blocked by: none.
- [ ] **T2** — A stored mode puts one provider first. Add the priority mode file with `load` and
  `save`. With mode `openai`, openai's entries are tried first and the rest after, each group
  in written order; a list without openai is unchanged; a denied openai entry falls to the next
  with the same verdict as in auto; nothing admitted still waits. The mode orders every
  track's stage lists, including a target's own policy. Review puts implement's provider last
  after the mode's order. A missing file, an unreadable file, and a stored provider that is not
  routed all behave as auto. The stored mode is read again on each pass, so it survives a
  restart and a change applies from the next pass. A stage with a pick keeps it across a mode
  change, and the next stage follows the new mode. The dispatcher's status lines apply a fixed
  mode with no usage reading. Add **Priority mode** to `CONTEXT.md`. Covers "an unreadable
  stored mode behaves as auto", "a stored provider that is no longer routed behaves as auto"
  (dispatcher halves), "a fixed mode puts its provider first", "a fixed mode whose provider has no usage
  reading uses the other provider", "a fixed mode keeps the written order inside each group", "a fixed mode leaves a list without that provider unchanged", "a
  denied priority entry falls to the next one", "nothing admitted still waits", "review avoids
  the implement provider under a fixed mode", "review follows the mode when implement ran on
  the other provider", "the mode orders a target's own policy", "a stage with a pick is not
  rerouted", "the next stage follows the new mode", "a one-shot override beats the mode", "a
  fixed mode ignores the session-bound rule" and "the mode survives a restart" (dispatcher
  half). _Goal: the operator can make the box spend
  one subscription first._
  Seam: `dispatcher.main.run_pass(cfg, deps)` with the mode placed through
  `dispatcher.priority.save(state_dir, mode, actor=, now=)` or a hand-written file;
  `dispatcher.priority.load(state_dir, routed)` for the fallback cases. Blocked by: T1, T6.
- [ ] **T6** — In auto, a session-bound provider goes first. Add `session_week_share` to
  `targets.yaml` (mapping of provider to a share above 0 and at most 1, or the load fails;
  documented in `targets.example.yaml`). In auto, a provider whose remaining unscoped weekly
  quota is at least its spendable maximum has its entries tried first; the open session counts
  for the unused part of the threshold, every started 5 real hours after it counts as one
  session, and with no session window every started 5 hours from now counts. A provider with no
  share, no unscoped weekly window or unavailable usage is never session-bound. The gate still
  denies a session-bound provider at its session cap, and review still puts implement's
  provider last. Add **Session-bound** to `CONTEXT.md`. Covers "a session-bound provider goes
  first at the boundary", "one point under the spendable maximum follows required pace", "a
  partly used open session counts for what is left of it", "with no session window reported,
  every started 5 hours counts", "a session-bound provider at its session cap is still denied",
  "no session week share means no session-bound rule", "review still avoids a session-bound
  implement provider" and "a session week share out of range fails the config load". _Goal:
  the subscription that can only be spent session by session is not left behind in auto._
  Seam: `dispatcher.main.run_pass(cfg, deps)`; `dispatcher.usage.session_bound(usage, now,
  cfg)` for the counting cases; `dispatcher.config.load_config(path)` for the key. Blocked by:
  T1.
- [ ] **T3** — Triage follows the mode. The triage sweep runs on the first admitted entry of
  `models.triage` in the mode's order (auto and fixed), and when none is admitted its "skipped
  — usage gate" note names the first entry in that order. Covers "the mode orders the triage
  list". _Goal: one rule for every list the box routes._
  Seam: `dispatcher.triage.run_sweep(cfg, deps, run)`. Blocked by: T2.
- [ ] **T4** — The console sets and reports the mode. `POST /api/priority` stores the mode,
  appends the event `priority-mode-set` with the operator's login and `mode=<mode>`, and
  refuses a mode that is neither `auto` nor a routed provider (422, nothing stored, no event)
  and a request with no operator session. `GET /api/usage` and `GET /api/board` report the
  current mode (auto when the file is missing, unreadable or names an unrouted provider), the
  options (`auto` plus the routed providers), the provider marked first, and the required pace
  of each weekly window; session cap, weekly allowance and weekly headroom are the same values
  whatever the mode. The board's next-launch model for a task with no pick is the one the
  dispatcher launches under the mode; the board snapshot, which has no usage reading, applies a
  fixed mode. Regenerate `frontend/src/lib/api-types.ts`. Covers the console halves of the
  three "behaves as auto" scenarios, "the operator sets a fixed mode", "the mode survives a
  restart" (console half), "the selector offers only auto and routed providers" (API half),
  "an unknown mode is refused", "an unauthenticated change is refused", "the console marks the
  leading provider in auto", "equal required pace marks anthropic first", "no required pace
  anywhere marks anthropic first", "the console marks a session-bound provider first", "the console marks the fixed provider" (API halves), "the mode
  does not change the gate's numbers" and "the console's next launch matches the dispatcher".
  _Goal: the operator picks the mode in the console and sees what it is doing._
  Seam: `web.app.create_app(...)` through `TestClient`: `POST /api/priority`, `GET /api/usage`,
  `GET /api/board`; the stored mode read back with `dispatcher.priority.load`. Blocked by: T2.
- [ ] **T5** — The usage panel has the selector. The panel header shows a segmented control with
  `Auto` and one option per routed provider, the current mode selected; choosing an option
  posts it and the panel shows the new mode. The provider reported as first carries a "first"
  chip, and only that one. Each weekly window shows its required pace as `N.N× pace` beside its
  headroom; a session window and a weekly window with none show nothing. A refused change
  leaves the selection where it was and shows the error. The control is usable at phone width
  with no horizontal scroll. Covers the UI halves of "the operator sets a fixed mode", "the
  selector offers only auto and routed providers", "the console marks the leading provider in
  auto" and "the console marks the fixed provider". _Goal: the choice is one tap, next to the
  numbers that motivate it._
  Seam: `frontend/src/components/UsagePanel.tsx` in vitest with fixtures; one Playwright flow
  in `frontend/e2e` against `fake-api.mjs` (which gains `POST /api/priority`). Blocked by: T4.
