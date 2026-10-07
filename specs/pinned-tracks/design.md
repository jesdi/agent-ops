# Pinned tracks: the kind of work chooses the model — Design

## Decisions

- **The pin is applied at the one ordering point.** `dispatcher.models.candidates` already
  receives the priority order for every stage list; for a track the policy's pinned list names
  it does not apply that order. No caller changes, and the dispatcher, the status lines and
  the console get the same answer — the review reorder stays where it is, after it.
- **`models.pinned` is parsed into the policy, not into the track.** `ModelPolicy` carries the
  ordered names; a target's own `models:` block is a whole policy, so it has its own list — a
  target that replaces the block and writes no list has no pinned track.
- **The track list puts pinned tracks first and adds the rules only when a track is pinned.**
  `tracks_text` is the one text both the triage sweep and the spec session get, so both are
  told the same thing — with no pinned list the text is exactly today's.
- **A ticket names its track on its own line, `Track: <name>`.** Read like the `Blocked by`
  line the ticket check already requires, at most one per ticket; two lines in one ticket make
  the set invalid — a sentence in the ticket body that starts a line with "Track:" is read as
  the track line.
- **Ticket tracks are checked when the plan stage signals done, by the ticket check.** An
  invalid set takes the path every invalid ticket set takes today: the plan session is resumed
  with the reason, and after the plan retry limit the task fails — no new park or retry path.
- **Ticket tracks are copied into the task state when the ticket set is accepted.** The
  dispatcher and the console route from the copy, not from the ticket files, so a ticket file
  an implement session edits later cannot move a ticket to another model, and the console
  needs no access to the worktree — a hand edit of a ticket after the plan stage has no
  effect on routing. Every accepted ticket set replaces the whole copy.
- **A ticket track that is no longer usable when its ticket starts parks the task.** If
  `targets.yaml` changed after the plan stage and the track is gone or no longer pinned, the
  ticket is not started on any other list; the task parks for the operator with the reason.
- **The implement pick is the pick of the ticket in progress.** It is present exactly while a
  ticket is in progress and is dropped when that ticket is done, so a task that waits between
  two tickets has no implement pick: the next ticket chooses from its own list, the console
  names that choice, and a one-shot override may name any provider — two state writes at a
  ticket boundary instead of one.
  - *As built.* The pick is the marker: a task in implement with an implement pick has a
    ticket in progress; with none it waits for its next ticket, or for review after the
    last one. A step that is done is persisted in a write of its own BEFORE the next launch
    is chosen and admitted, so every reader of the state names the launch that waits. Two
    transitions are persisted this way: "plan done, ticket set accepted" (the task is in
    implement with no ticket started and the set's count, file names and ticket tracks)
    and "ticket done" (the pick is dropped; after the last ticket the next launch is
    review). The plan session is ended at acceptance, so it cannot change the set while
    ticket 1 waits. The accepted file names are the identity of the set; they come from
    the one read of the directory that checked the set. A ticket is launched from the file
    with its accepted name, never from a position; when that file was renamed or replaced
    the task parks, and when it is gone the launch fails as a missing ticket. Review starts
    only when the directory holds exactly the accepted names, on every path that starts it
    (the drive loop, a wake, a crash resume). Nothing accepts a changed set: the task parks
    again until the accepted files are back, or the operator cancels it. A set accepted
    before the names were kept is checked by its count before review. A state
    file from before picks existed has a ticket in progress with no pick; the state read
    marks it (`ticket_without_pick`), and the mark is cleared when that ticket is done.
- **The providers that ran tickets are recorded on the task.** One provider is added when a
  ticket's first session is launched; review avoids the single recorded provider, or none when
  there is more than one — a provider whose session started and wrote nothing still counts,
  which errs toward "avoid none".
- **PR feedback has its own pick key.** Address-review sessions stop reading and writing the
  implement pick; the first one chooses from the task track's `implement` list and stores the
  choice under its own key, which every later round reuses — one more key in `picks`.
- **Tasks in flight are migrated when their state is read.** A task past implement that still
  carries an implement pick has its provider moved into the recorded implement providers, and,
  when it has an open PR, the pick moved to the feedback key, so its next feedback round stays
  on the provider its sessions ran on — the migration is one way; rolling the code back leaves
  those tasks without an implement pick.
- **The console's views carry the pin; the pages do not derive it.** The task card and the
  task's admission view name the pinned track the next launch comes from (the ticket track
  when the next launch is a ticket that has one, else the task track when it is pinned), and
  the priority view lists the global policy's pinned tracks — a target with its own pinned
  list is shown correctly on its tasks but not in the usage panel line.
- **The override list for a pinned wait is the target policy's models.** The console's task
  admission view offers `ModelPolicy.model_ids()` when the next launch comes from a pinned
  track, and the track's own entries otherwise; the run and resume routes already accept any
  model of the policy, and `override_refusal` keeps the same-provider limit once a pick
  exists — the operator can put a model on a lane it was never written for, one launch at a
  time.
- **A pinned task that waits keeps its capacity unit, as every waiting task does today.** No
  auto-park for a long pinned wait — with capacity 2, one pinned task that waits for its
  provider's weekly reset halves the box until then.
- **`CONTEXT.md` gains "Pinned track" and "Ticket track"**, corrects "Pick" (per ticket for
  implement, one for PR feedback), and the routing comment in `targets.example.yaml` says that
  the mode does not reorder a pinned track.

## Data model

No database. One `targets.yaml` key, fields in the per-task state file, one line in a ticket
file, and fields on the console's views.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `targets.yaml` → `ModelPolicy` | `pinned` | list of names; each a track of the same policy; no name twice; absent or empty = none; anything else fails the config load | which tracks the mode does not reorder, and their precedence |
| ticket file `NN-slug.md` | `Track: <name>` line | optional; at most one; the name is a pinned track other than `security`; none at all when the task track is `security`; otherwise the ticket set is invalid | the ticket track |
| `TaskState` | `ticket_tracks` | mapping of ticket number to track name; only tickets that name one; replaced as a whole each time a ticket set is accepted; empty for a task planned before this change | what the dispatcher and the console route from |
| | `ticket_names` | file names of the accepted ticket set, in ticket order; empty for a set accepted before the names were kept (then the count is checked before review) | the identity of the accepted set |
| | `picks["implement"]` | present exactly while a ticket is in progress; dropped when the ticket is done | the pick of the ticket in progress (was: of the whole stage) |
| | `picks["feedback"]` | set by the first address-review launch; never changed after | the one pick for every PR feedback round |
| | `implement_providers` | list of provider names without repeats, in the order first used; a provider is added when a ticket's first session is launched | review avoids the single one, or none |
| | state read | a task past implement with `picks["implement"]`: provider added to `implement_providers`, the pick moved to `picks["feedback"]` when the task has an open PR, and removed otherwise | tasks in flight at deploy keep their provider |
| `TaskCard`, `TaskAdmissionView` | `pinned_track` | a pinned track name, or empty when the next launch does not come from a pinned track | "pinned (track)" next to the model and in the wait reason |
| `PriorityView` | `pinned` | the global policy's pinned list, in its order; empty when none | the line under the priority control |

Unchanged: `provider-priority.json`, `execution-overrides/`, `picks["spec"]`, `picks["plan"]`,
`picks["review"]`, the usage gate and its inputs.

## Identities

The implement pick is rekeyed from "the task's implement stage" to "the ticket in progress",
and PR feedback leaves it. Everything built on the old key:

- `dispatcher/main.py::_spawn_stage` writes `picks[policy_stage(stage)]`, which is
  `implement` for an address-review launch too — covered by T4.
- `dispatcher/main.py` resume path (`resumed` event) writes the same key — covered by T4
  (address-review) and T6 (a ticket in progress keeps its pick).
- `dispatcher/main.py::_launch_for` reads the stage pick before resolving — covered by T4
  (feedback key) and T6 (no pick between tickets, list of the ticket track).
- `dispatcher/main.py::_launch_for` review avoid, `pick_provider(picks, "implement")` —
  covered by T5.
- `web/app.py::_avoid`, the same read in the console — covered by T5.
- `dispatcher/main.py::_display_entry` (status lines) reads the stage pick, else the first
  candidate of the task track — covered by T6.
- `web/app.py::_model_for`, `_choices`, `_task_admission` (next-launch model, alternatives,
  `any_provider`) — covered by T4 (feedback) and T6 (ticket track, no pick between tickets).
- `dispatcher.models.override_refusal` / `override_allowed`, called from the console's run and
  resume routes and from the dispatcher's intent validation: "a stage's provider is fixed once
  it has a pick" — covered by T4 (address-review checks the feedback pick) and T6 (between
  tickets there is no pick to fix a provider).
- `dispatcher/main.py::_report_session_crash` reads the stage pick to name the runtime —
  covered by T4 (a crashed address-review session reads the feedback pick).
- `dispatcher.models._POLICY_STAGES` maps `address-review` to `implement` for both the list
  and the pick key — covered by T4 (the list mapping stays, the pick key separates).
- State files of tasks in flight that carry `picks["implement"]` past implement — covered by
  T4 (the read-time migration).
- `.agent/models.log` in the worktree: one appended line per launch with stage and model —
  out of scope: it is a log of launches, not a key, and each ticket launch already adds a line.
- Events `stage-started`, `ticket-started`, `resumed` carry the launched model — out of scope:
  one event per launch, nothing reads a "the implement model" back from them.
- `execution-overrides/` and `TaskState.resume_model_override` — out of scope: one-shot, keyed
  by task, consumed by the next launch whichever ticket it is.

## Seams

- `dispatcher.config.load_config(path)` — existing; the new `models.pinned` key, globally and
  in a target's own block.
- `dispatcher.models.parse_policy(raw)` — existing; `ModelPolicy.pinned: tuple[str, ...]`.
- `dispatcher.models.candidates(policy, track, stage, avoid_provider="", *, order)` and
  `resolve(...)` — existing, same signature; a pinned track ignores `order`.
- `dispatcher.models.tracks_text(policy)` — existing; pinned tracks first, marked, with the
  rules.
- `dispatcher.artifacts.check_tickets(tickets_dir, ...)` — existing; gains the names a ticket
  may use (none for a `security` task) and returns the ticket tracks with the count.
- `dispatcher.state.TaskState` read and write — existing; the new fields and the read-time
  migration, driven through the state file round trip.
- `dispatcher.main.run_pass(cfg, deps)` — existing; the high seam for every launch scenario:
  usage with the helpers in `tests/usagefakes.py`, the mode with `dispatcher.priority.save`,
  tickets as files in the task's worktree, the launched model read from the sessions fake and
  from the task state.
- `dispatcher.triage.run_sweep(cfg, deps, run)` — existing; the model the sweep runs on and
  the track list in its prompt.
- The rendered plan prompt (`prompts/plan.md` through the dispatcher's prompt rendering) —
  existing; the ticket-track instruction.
- `web.app.create_app(...)` through FastAPI's `TestClient`: `GET /api/board`, the task detail
  route and `GET /api/usage` — existing; the new `pinned_track` and `pinned` fields.
- `frontend/src/components/UsagePanel.tsx` and the task card and task detail components in
  vitest with fixtures — existing.
- `frontend/e2e` Playwright against `fake-api.mjs` — existing; the fake gains the new fields.
