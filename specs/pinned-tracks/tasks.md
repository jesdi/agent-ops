# Pinned tracks: the kind of work chooses the model — Tasks

Each task links to the goal in proposal.md: a track can be pinned, so its lists run in written
order whatever the priority mode, and a ticket can name its own pinned track for the implement
stage. Work the frontier (tasks whose blockers are done), commit per task with its ID, and
finish every task with `make gate` green; tasks that touch `frontend/` also finish with
`pnpm test:e2e` green there. Tests come first in every task.

- [ ] **T1** — The priority mode does not reorder a pinned track. Add `models.pinned` to the
  policy (a list of track names of the same policy, none twice, or the config load fails;
  absent or empty means none; a target's own `models:` block has its own list). For a pinned
  track every stage list is tried in written order under `auto`, under a provider-first mode
  and when a provider is session-bound; unpinned tracks and the triage list are ordered by the
  mode as today. The gate, picks and one-shot overrides behave as on any track, and review
  still puts implement's provider last. The status lines name the same entry. Add **Pinned
  track** to `CONTEXT.md`. Covers "a track the pinned list does not name is unpinned", "with
  no pinned list no track is pinned", the three "a pinned list that … fails the config load"
  scenarios, "a target's own policy has its own pinned list", "auto does not reorder a pinned
  track", "a provider-first mode does not reorder a pinned track", "the session-bound rule
  does not reorder a pinned track", "an unpinned track is still ordered by the mode in the
  same pass", "triage is still ordered by the mode", "a pinned track falls back in written
  order", "a pinned track waits when none of its entries is admitted", "a single-entry pinned
  list waits", "a pick on a pinned track is kept", "a one-shot override wins on a pinned
  track" and both "review of a pinned track …" scenarios. _Goal: the kind of work, not the
  quota, chooses the model on the tracks the operator pins._
  Seam: `dispatcher.main.run_pass(cfg, deps)` for launches;
  `dispatcher.config.load_config(path)` for the config cases;
  `dispatcher.triage.run_sweep(cfg, deps, run)` for triage. Blocked by: none.
- [ ] **T2** — The example config carries the lanes and sessions are told how to choose. In
  `targets.example.yaml` add the `architecture` track, set the `frontend` and `security`
  lists from the spec, write `pinned: [security, architecture, frontend]`, and correct the
  routing comment. The track list shown to the triage sweep and to the spec session lists the
  pinned tracks first, in pinned-list order, each marked pinned, then the rest, with the three
  rules (only when the work clearly fits; else the untracked track; first listed wins); with
  no pinned track the text is today's. Covers "the example config defines the lanes", "the
  track list tells triage and the spec session how to choose" and "with no pinned track the
  track list is as today". _Goal: the operator's lanes exist, and the sessions that choose a
  track know the precedence._
  Seam: `dispatcher.config.load_config("targets.example.yaml")`;
  `dispatcher.models.tracks_text(policy)`, and the prompt the triage sweep and a spec launch
  receive through `run_sweep` and `run_pass`. Blocked by: T1.
- [ ] **T3** — The console shows the pin. The board and the task detail name the pinned track
  next to the model when the task's next launch comes from a pinned track, the wait reason of
  a task denied by the usage gate says that it is pinned to that track, and the usage panel
  shows one line under the priority control that lists the pinned tracks, or no line when
  there is none. For a pinned task that waits on the usage gate, the wait reason offers every
  model of the target's policy for a one-shot override (only the pick's provider once a pick
  exists); an unpinned task offers its own list as today. Check the usage panel and a pinned task card at 390 px and at desktop width,
  light and dark. Covers "the console marks a pinned task", "the console explains a pinned
  wait", "the usage panel lists the tracks the mode does not apply to" and "with no pinned
  track the usage panel shows no such line", "the console offers every configured model for a
  pinned wait", "an unpinned wait offers its own list as today" and "with a pick, only that
  provider's models are offered" (the ticket-in-progress state given in the task state). _Goal: the operator sees why a task ignores the
  priority mode._
  Seam: `GET /api/board`, the task detail route and `GET /api/usage` through `TestClient`;
  `UsagePanel.tsx` and the task card in vitest; Playwright against `fake-api.mjs`.
  Blocked by: T1.
- [ ] **T4** — PR feedback has one pick. The first address-review session chooses from the
  task track's `implement` list (written order when pinned, the mode's order when not) and
  that choice is stored under its own pick key and reused by every later feedback round; a
  denied feedback pick waits. Address-review no longer reads or writes the implement pick: the
  override rule, the crash report and the console's next-launch model use the feedback pick. A
  task read from state that is past implement and still carries an implement pick keeps its
  provider: the pick becomes the feedback pick when the task has an open PR. Correct **Pick**
  in `CONTEXT.md`. Covers "PR feedback chooses from the task track's implement list" (with
  the implement providers given in the task state), "the PR feedback pick is reused for every
  round" and "PR feedback waits when its pick is denied". _Goal: frees the implement pick to
  belong to one ticket, without moving feedback to another model mid-PR._
  Seam: `dispatcher.main.run_pass(cfg, deps)` with a task at `pr-open` and feedback queued;
  the `TaskState` file round trip for the migration; `GET /api/board` for the next-launch
  model. Blocked by: T1.
- [ ] **T5** — Review avoids one implement provider, or none. Record on the task the providers
  that ran its tickets, added when a ticket's first session is launched. Review moves the
  single recorded provider's entries to the back; with more than one recorded it moves
  nothing, and its list is in written order (pinned) or the mode's order (unpinned). Two
  models of one provider are one provider. A task read from state with an implement pick and
  no recorded provider counts that pick's provider. The console's next-launch model for a
  review follows the same rule. Covers "review avoids the one implement provider", "review
  avoids none when implement used both providers", "with both providers used, an unpinned
  review list follows the mode" and "two models of one provider count as one implement
  provider" (the mixed cases with the recorded providers given in the task state).
  _Goal: review independence survives a task whose tickets ran on two providers._
  Seam: `dispatcher.main.run_pass(cfg, deps)` with a task entering `review`;
  `GET /api/board`. Blocked by: T1.
- [ ] **T6** — A ticket can name a pinned track for the implement stage. The plan prompt tells
  the plan session that a ticket may carry one `Track: <name>` line, lists the names it may
  use (the pinned tracks other than `security`), and says that a `security` task takes none.
  When the plan stage signals done, a ticket that names an unknown, unpinned or `security`
  track, names two tracks, or names any track in a `security` task makes the ticket set
  invalid: the plan session is sent back with a reason that names the ticket and the track,
  and no ticket is implemented. An accepted set's ticket tracks are stored on the task. Each
  ticket chooses its entry from its ticket track's `implement` list, or from the task track's
  when it has none, and keeps that pick for its own sessions; the pick is dropped when the
  ticket is done, so the next ticket chooses again. A ticket with no admitted entry is not
  started and the tickets after it wait. A one-shot override still wins. A ticket whose
  stored track is no longer a pinned track of the policy parks the task for the operator.
  Spec, plan and review use the task track. The status lines and the console name the model
  and the pinned track of the next ticket. Add **Ticket track** to `CONTEXT.md`. Covers "a
  ticket track chooses the list for that ticket only", "a ticket track on an unpinned task",
  "spec, plan and review ignore ticket tracks", "a ticket that names an unpinned track is
  refused", "a ticket that names an unknown track is refused", "a ticket cannot name the
  security track", "a security task takes no ticket track", "a security task with no ticket
  track runs every ticket on its own list", "a ticket keeps its pick across its own
  sessions", "the next ticket chooses again", "a ticket that cannot run holds the tickets
  after it" and "a ticket track that is no longer pinned parks the task", "an edit of a ticket file after
  the plan stage does not move the ticket", "a one-shot override wins over a ticket track" and "an override of a pin covers one
  ticket". _Goal: each part of a mixed
  task runs on the model chosen for that kind of work._
  Seam: `dispatcher.main.run_pass(cfg, deps)` with ticket files in the task's worktree;
  `dispatcher.artifacts.check_tickets(tickets_dir, ...)` for the validation cases; the
  rendered plan prompt; `GET /api/board`. Blocked by: T4, T5.
