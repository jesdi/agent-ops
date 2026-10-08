# Pinned tracks: the kind of work chooses the model — Spec

> Superseded in part by `specs/2026-10-06-openspec-pipeline/` and
> `docs/adr/0003-implement-is-one-session.md`: implement is one session per
> task, so requirements 7, 8 and 9 (ticket tracks and a pick per ticket) and
> the per-ticket parts of requirements 10 and 13 no longer hold. A task's pin
> is its own track, for every stage. This file is the record of the change
> as it was built; the code is the present state.

Terms:

- The **pinned list** is `models.pinned` in `targets.yaml`: a list of track names. A **pinned
  track** is a track it names; any other track is **unpinned**. The order of the list is the
  **precedence** of the pinned tracks: the first named wins when more than one fits.
- The **written order** of a stage list is the order of its entries in `targets.yaml`.
- The **task track** is the track a task carries (triage's label for the spec stage, then the
  spec session's choice).
- A **ticket track** is a track that one ticket names for itself. A ticket that names none has
  no ticket track.
- The **implement providers** of a task are the providers of the entries that ran its tickets.

Priority mode, required pace, session-bound, entry, pick and one-shot override are as in
`specs/provider-priority` and `CONTEXT.md`.

Unless a scenario says otherwise, the policy has `pinned: [security, architecture, frontend]`,
`untracked: standard`, and these tracks:

```yaml
standard:
  spec:      [claude-fable-5-1@medium, openai/gpt-astra@medium]
  plan:      [claude-fable-5-1@medium, openai/gpt-astra@medium]
  implement: [claude-sonnet-5@medium, openai/gpt-sol@medium]
  review:    [openai/gpt-astra@medium, claude-opus-5@medium]
frontend:
  spec:      [claude-fable-5-1@medium, claude-opus-5@medium]
  plan:      [claude-fable-5-1@medium, claude-opus-5@medium]
  implement: [claude-fable-5-1@medium, claude-opus-5@medium]
  review:    [openai/gpt-astra@medium, claude-opus-5@medium]
architecture:
  spec:      [openai/gpt-astra@high, claude-fable-5-1@high]
  plan:      [openai/gpt-astra@high, claude-fable-5-1@high]
  implement: [openai/gpt-astra@medium, claude-fable-5-1@medium]
  review:    [claude-fable-5-1@high, openai/gpt-astra@high]
security:
  spec:      [openai/gpt-astra@high]
  plan:      [openai/gpt-astra@high]
  implement: [openai/gpt-astra@high]
  review:    [claude-fable-5-1@high, openai/gpt-astra@high]
```

and "anthropic is ahead" means: mode `auto`, anthropic's weekly window is 20% used and resets
in 1 day (required pace 5.6×), openai's weekly window is 10% used and resets in 4 days (1.6×),
so `auto` puts anthropic entries first on an unpinned list.

## Requirements

1. **The pinned list names the pinned tracks.** `models.pinned` is a list of track names, each
   a track of the same policy, with no name twice; anything else fails the config load. Absent
   or empty means no track is pinned. A target's own policy carries its own pinned list; the
   global list does not apply to it.
2. **The priority mode does not reorder a pinned track.** For every stage of a pinned track,
   the entries are tried in written order: required pace, the session-bound rule and a
   provider-first mode are not applied to it. Unpinned tracks and `models.triage` are ordered
   by the mode as today.
3. **The gate, picks and overrides are unchanged.** On a pinned track the box launches the
   first entry in written order that the usage gate admits and waits when none is admitted. A
   stage that has a pick keeps it. A one-shot override launches the model it names.
4. **Review independence still applies to a pinned track.** For the review stage of a pinned
   track, requirement 10 is applied to the written order.
5. **The example config carries the operator's lanes.** `targets.example.yaml` defines the
   tracks shown above: `frontend`, `architecture` and `security` pinned with those lists,
   `trivial` and `standard` unpinned. `architecture` is described as heavy backend work or a
   change to the architecture (module boundaries, data model or schema, persistence,
   concurrency, a refactor across modules, a public interface other code depends on).
6. **Sessions are told how to choose among pinned tracks.** The track list shown to triage and
   to the spec session shows the pinned tracks first, in the order of the pinned list, each
   marked as pinned, then the unpinned tracks. It carries these rules: pick a pinned track only
   when the work clearly fits its sentence; when it is not clear, pick the `untracked` track;
   when more than one pinned track fits, pick the one listed first. The example config's
   pinned list is `[security, architecture, frontend]`. The existing rule that security work is
   never below the security track stays.
7. **A ticket can name a pinned track for the implement stage.** The plan session may give a
   ticket a ticket track. A ticket with a ticket track is implemented from that track's
   `implement` list; a ticket without one is implemented from the task track's `implement`
   list. Spec, plan and review always use the task track.
8. **A ticket track is limited.** A ticket track must be a configured, pinned track other than
   `security`, and no ticket of a task whose task track is `security` may have a ticket track.
   A ticket set that breaks this is not a valid ticket set: the plan stage is sent back, as it
   is for any other invalid ticket set, and no ticket is implemented.
9. **Each ticket has its own pick.** The entry chosen when a ticket starts is kept for every
   session of that ticket (a gate-loop round, a resume after a park). The next ticket chooses
   again from its own list. A ticket whose list has no admitted entry waits, and the tickets
   after it wait with it.
   The ticket tracks are fixed when the ticket set is accepted: a later edit of a ticket file
   does not change them. A ticket whose track is no longer a pinned track of the policy when
   the ticket starts is not started: the task parks for the operator with a reason that names
   the ticket and the track.
10. **Review avoids one implement provider, or none.** When all tickets of a task ran on one
    provider, review moves that provider's entries to the back, as today. When the tickets ran
    on more than one provider, review moves nothing: its list is used in the order
    requirement 2 or the priority mode gives it.
11. **The console shows the pin.** A task whose next launch comes from a pinned track shows the
    track as pinned next to the model, and a task that waits on the usage gate for a pinned
    track's entry says so in its wait reason. The usage panel lists, under the priority
    control, the pinned tracks as the tracks the mode does not apply to; with no pinned track
    it shows no such line. The model the console names as a task's next launch is the one the
    dispatcher would launch.
12. **PR feedback has one pick.** The first session that addresses PR feedback chooses from
    the task track's `implement` list (in written order when that track is pinned, in the
    priority mode's order when it is not), whatever the tickets ran on. That pick is kept for
    every later feedback session of the task. Ticket tracks do not apply to PR feedback.
13. **The operator can override a pin from the console.** For a task whose next launch comes
    from a pinned track and is denied by the usage gate, the console offers every model that
    the target's policy names, not only the entries of the pinned list, each with its usage
    verdict. Choosing one is the existing one-shot override: it covers the next launch only,
    so for the implement stage it covers one ticket, and the next ticket chooses again from
    its own list. The existing limit stays: once the stage or the ticket in progress has a
    pick, or PR feedback has its pick, only models of that pick's provider are offered and
    accepted. For a task on an unpinned track the console offers what it offers today.

## Scenarios

### Scenario: a track the pinned list does not name is unpinned

- **Given** the policy above, in which the pinned list does not name `standard`, and anthropic
  is ahead
- **When** a `standard` task enters `review` with no implement pick and the gate admits both
  entries
- **Then** it launches `claude-opus-5@medium`

### Scenario: with no pinned list no track is pinned

- **Given** the policy above without the `pinned` key, and anthropic is ahead
- **When** an `architecture` task enters `implement` and the gate admits both entries
- **Then** it launches `claude-fable-5-1@medium`

### Scenario: a pinned list that names an unknown track fails the config load

- **Given** `targets.yaml` with `pinned: [security, backend]` and no track named `backend`
- **When** the config is loaded
- **Then** the load fails with a message that names `pinned` and `backend`

### Scenario: a pinned list that names a track twice fails the config load

- **Given** `targets.yaml` with `pinned: [frontend, security, frontend]`
- **When** the config is loaded
- **Then** the load fails with a message that names `pinned` and `frontend`

### Scenario: a pinned list that is not a list of names fails the config load

- **Given** `targets.yaml` with `pinned: frontend`
- **When** the config is loaded
- **Then** the load fails with a message that names `pinned`

### Scenario: a target's own policy has its own pinned list

- **Given** the global policy above; target `example-app` has its own `models:` block with
  `pinned: [standard]`, `standard` `implement: [openai/gpt-sol@medium,
  claude-sonnet-5@medium]`, and an `architecture` track with the lists above that its pinned
  list does not name; anthropic is ahead
- **When** an `example-app` task on `standard` and an `example-app` task on `architecture`
  enter `implement` and the gate admits every entry
- **Then** the `standard` task launches `openai/gpt-sol@medium` (pinned for this target) and
  the `architecture` task launches `claude-fable-5-1@medium` (not pinned for this target)

### Scenario: auto does not reorder a pinned track

- **Given** anthropic is ahead
- **When** an `architecture` task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-astra@medium`

### Scenario: a provider-first mode does not reorder a pinned track

- **Given** mode `anthropic`
- **When** an `architecture` task enters `plan` and the gate admits both entries
- **Then** it launches `openai/gpt-astra@high`

### Scenario: the session-bound rule does not reorder a pinned track

- **Given** mode `auto`, `session_week_share: {anthropic: 0.05}`, and anthropic is
  session-bound (weekly window 20% used, resets in 10 h, no open session: 2 sessions left,
  spendable maximum 10%, remaining quota 80%)
- **When** an `architecture` task enters `implement` and the gate admits both entries
- **Then** it launches `openai/gpt-astra@medium`

### Scenario: an unpinned track is still ordered by the mode in the same pass

- **Given** anthropic is ahead, an `architecture` task and a `standard` task both enter
  `review` in one pass with no implement pick, and the gate admits every entry
- **When** the pass runs
- **Then** the `architecture` task launches `claude-fable-5-1@high` (written first) and the
  `standard` task launches `claude-opus-5@medium` (written second, moved first by `auto`)

### Scenario: triage is still ordered by the mode

- **Given** `models.triage: [claude-sonnet-5@medium, openai/gpt-luna@medium]`, mode `openai`,
  and the pinned list above
- **When** the triage sweep runs and the gate admits both entries
- **Then** it runs on `openai/gpt-luna@medium`

### Scenario: a pinned track falls back in written order

- **Given** mode `anthropic`, and the gate denies `openai/gpt-astra` and admits
  `claude-fable-5-1`
- **When** an `architecture` task enters `implement`
- **Then** it launches `claude-fable-5-1@medium`

### Scenario: a pinned track waits when none of its entries is admitted

- **Given** mode `openai`, the gate denies `claude-fable-5-1` and `claude-opus-5` and admits
  every openai model
- **When** a `frontend` task enters `implement`
- **Then** nothing is launched for it and the task waits; no openai model runs it

### Scenario: a single-entry pinned list waits

- **Given** anthropic is ahead, the gate denies `openai/gpt-astra` and admits every anthropic
  model
- **When** a `security` task enters `implement`
- **Then** nothing is launched for it and the task waits

### Scenario: a pick on a pinned track is kept

- **Given** an `architecture` task whose `plan` stage has the pick `claude-fable-5-1@high`, and
  the gate now admits both entries
- **When** the `plan` stage is resumed
- **Then** it launches `claude-fable-5-1@high`

### Scenario: a one-shot override wins on a pinned track

- **Given** a `frontend` task about to enter `implement`, and the operator's one-shot override
  names `openai/gpt-astra`
- **When** the task enters `implement`
- **Then** it launches `openai/gpt-astra`

### Scenario: review of a pinned track avoids the implement provider

- **Given** an `architecture` task whose every ticket ran on `claude-fable-5-1@medium`
- **When** it enters `review` and the gate admits both entries
- **Then** it launches `openai/gpt-astra@high`

### Scenario: review of a pinned track keeps the written order when the first entry is independent

- **Given** mode `openai`, and an `architecture` task whose every ticket ran on
  `openai/gpt-astra@medium`
- **When** it enters `review` and the gate admits both entries
- **Then** it launches `claude-fable-5-1@high`

### Scenario: the example config defines the lanes

- **Given** `targets.example.yaml` with its commented keys left as they are
- **When** it is loaded as the config
- **Then** the load succeeds; the pinned list is `[security, architecture, frontend]`, so
  `trivial` and `standard` are unpinned; `architecture` has exactly the lists shown above; no
  `security` list for spec, plan or implement holds an anthropic entry; no `frontend` list for
  spec, plan or implement holds an openai entry; no `architecture` list holds a model other
  than `gpt-astra` and `claude-fable-5-1`

### Scenario: the track list tells triage and the spec session how to choose

- **Given** the policy above, with the tracks written in the order `standard`, `frontend`,
  `architecture`, `security`
- **When** the track list is built for the triage sweep and for a spec session
- **Then** the lines appear in the order `security`, `architecture`, `frontend`, `standard`;
  the first three are marked pinned and `standard` is not; the text says to pick a pinned
  track only when the work clearly fits, to pick `standard` otherwise, and to pick the first
  listed when more than one pinned track fits

### Scenario: with no pinned track the track list is as today

- **Given** a policy in which no track is pinned
- **When** the track list is built
- **Then** no line is marked pinned and the text carries none of the pinned-track rules

### Scenario: a ticket track chooses the list for that ticket only

- **Given** mode `openai`, an `architecture` task with 3 tickets, ticket 02 has the ticket
  track `frontend`, tickets 01 and 03 have none, and the gate admits every entry
- **When** the three tickets are implemented
- **Then** ticket 01 runs on `openai/gpt-astra@medium`, ticket 02 on `claude-fable-5-1@medium`
  and ticket 03 on `openai/gpt-astra@medium`

### Scenario: a ticket track on an unpinned task

- **Given** anthropic is ahead, a `standard` task with 2 tickets, ticket 01 has the ticket
  track `architecture`, ticket 02 has none, and the gate admits every entry
- **When** the two tickets are implemented
- **Then** ticket 01 runs on `openai/gpt-astra@medium` and ticket 02 on
  `claude-sonnet-5@medium`

### Scenario: spec, plan and review ignore ticket tracks

- **Given** a `standard` task in mode `openai` whose every ticket has the ticket track
  `architecture` and ran on `openai/gpt-astra@medium`
- **When** it enters `review` and the gate admits every entry
- **Then** it launches from the `standard` review list with openai moved back:
  `claude-opus-5@medium` (the `architecture` review list would give `claude-fable-5-1@high`)

### Scenario: a ticket that names an unpinned track is refused

- **Given** an `architecture` task whose plan session wrote 2 tickets, and ticket 02 names the
  track `standard`
- **When** the plan stage signals done
- **Then** the ticket set is not valid, the plan stage is sent back with a reason that names
  ticket 02 and track `standard`, and no ticket is implemented

### Scenario: a ticket that names an unknown track is refused

- **Given** a `standard` task whose ticket 01 names the track `backend`, which is not
  configured
- **When** the plan stage signals done
- **Then** the ticket set is not valid, the reason names ticket 01 and track `backend`, and no
  ticket is implemented

### Scenario: a ticket cannot name the security track

- **Given** a `standard` task whose ticket 01 names the track `security`
- **When** the plan stage signals done
- **Then** the ticket set is not valid and no ticket is implemented

### Scenario: a security task takes no ticket track

- **Given** a `security` task whose ticket 02 names the track `frontend`
- **When** the plan stage signals done
- **Then** the ticket set is not valid, the reason names ticket 02, and no ticket is
  implemented

### Scenario: a security task with no ticket track runs every ticket on its own list

- **Given** a `security` task with 2 tickets and no ticket track, and anthropic is ahead
- **When** the tickets are implemented and the gate admits `openai/gpt-astra`
- **Then** both run on `openai/gpt-astra@high`

### Scenario: a ticket keeps its pick across its own sessions

- **Given** an `architecture` task; ticket 02 has the ticket track `frontend` and started on
  `claude-opus-5@medium` because `claude-fable-5-1` was denied; ticket 02 then parked in its
  gate loop
- **When** ticket 02 is resumed and the gate now admits `claude-fable-5-1`
- **Then** it resumes on `claude-opus-5@medium`

### Scenario: the next ticket chooses again

- **Given** the task above, ticket 02 finished on `claude-opus-5@medium`, ticket 03 has no
  ticket track
- **When** ticket 03 starts and the gate admits every entry
- **Then** it runs on `openai/gpt-astra@medium`

### Scenario: a ticket that cannot run holds the tickets after it

- **Given** an `architecture` task with 3 tickets, ticket 01 done, ticket 02 has the ticket
  track `frontend`, the gate denies `claude-fable-5-1` and `claude-opus-5` and admits
  `openai/gpt-astra`
- **When** a dispatcher pass runs
- **Then** ticket 02 is not started, ticket 03 is not started, and the task waits in
  `implement`

### Scenario: a ticket track that is no longer pinned parks the task

- **Given** an `architecture` task with 3 tickets, ticket 01 done, ticket 02 accepted with the
  ticket track `frontend`; `targets.yaml` then changes to `pinned: [security, architecture]`
- **When** a dispatcher pass runs and the gate admits every entry
- **Then** ticket 02 is not started on any model, and the task is parked for the operator with
  a reason that names ticket 02 and track `frontend`

### Scenario: an edit of a ticket file after the plan stage does not move the ticket

- **Given** an `architecture` task with 2 tickets accepted with no ticket track; ticket 01 is
  in progress, and the line `Track: frontend` is then added to ticket 02's file
- **When** ticket 01 is done and ticket 02 starts, and the gate admits every entry
- **Then** ticket 02 runs on `openai/gpt-astra@medium`

### Scenario: a one-shot override wins over a ticket track

- **Given** an `architecture` task about to start ticket 02, which has the ticket track
  `frontend`, and the operator's one-shot override names `openai/gpt-astra`
- **When** ticket 02 starts
- **Then** it runs on `openai/gpt-astra`

### Scenario: review avoids the one implement provider

- **Given** a `standard` task in mode `openai` whose 3 tickets all ran on
  `openai/gpt-sol@medium`
- **When** it enters `review` and the gate admits both entries
- **Then** it launches `claude-opus-5@medium`

### Scenario: review avoids none when implement used both providers

- **Given** an `architecture` task whose ticket 01 ran on `openai/gpt-astra@medium` and ticket
  02 on `claude-fable-5-1@medium`
- **When** it enters `review` and the gate admits both entries
- **Then** it launches `claude-fable-5-1@high`, the first entry in written order

### Scenario: with both providers used, an unpinned review list follows the mode

- **Given** a `standard` task in mode `anthropic` whose ticket 01 ran on
  `openai/gpt-astra@medium` (ticket track `architecture`) and ticket 02 on
  `claude-sonnet-5@medium`
- **When** it enters `review` and the gate admits both entries
- **Then** it launches `claude-opus-5@medium`: the mode's order, with no entry moved back

### Scenario: two models of one provider count as one implement provider

- **Given** a `frontend` task whose ticket 01 ran on `claude-fable-5-1@medium` and ticket 02
  on `claude-opus-5@medium`
- **When** it enters `review` and the gate admits both entries
- **Then** it launches `openai/gpt-astra@medium`

### Scenario: the console marks a pinned task

- **Given** mode `openai`, a `frontend` task about to enter `implement` and a `standard` task
  about to enter `implement`, and the gate admits every entry
- **When** the operator opens the board
- **Then** the `frontend` task names `claude-fable-5-1` as its model and shows it as pinned to
  `frontend`; the `standard` task names `openai/gpt-sol` and shows no pin

### Scenario: the console explains a pinned wait

- **Given** a `frontend` task whose wake is denied because the gate denies `claude-fable-5-1`
  and `claude-opus-5`, while every openai model is admitted
- **When** the operator opens the task
- **Then** its wait reason says that the task is pinned to `frontend`

### Scenario: the usage panel lists the tracks the mode does not apply to

- **Given** the policy above
- **When** the operator opens the usage panel
- **Then** under the priority control it reads that the mode does not apply to `security`,
  `architecture` and `frontend`

### Scenario: with no pinned track the usage panel shows no such line

- **Given** a policy in which no track is pinned
- **When** the operator opens the usage panel
- **Then** the priority control has no line about pinned tracks

### Scenario: PR feedback chooses from the task track's implement list

- **Given** mode `anthropic`, an `architecture` task with an open PR whose ticket 01 ran on
  `openai/gpt-astra@medium` and ticket 02, with the ticket track `frontend`, on
  `claude-fable-5-1@medium`
- **When** the first PR feedback arrives and the gate admits every entry
- **Then** the feedback session runs on `openai/gpt-astra@medium`

### Scenario: the PR feedback pick is reused for every round

- **Given** the task above, whose first feedback session ran on `claude-fable-5-1@medium`
  because `openai/gpt-astra` was denied then
- **When** a second round of PR feedback arrives and the gate now admits every entry
- **Then** the second feedback session runs on `claude-fable-5-1@medium`

### Scenario: PR feedback waits when its pick is denied

- **Given** the task above, with the feedback pick `claude-fable-5-1@medium`, and the gate now
  denies `claude-fable-5-1` and admits `openai/gpt-astra`
- **When** a third round of PR feedback arrives
- **Then** no feedback session is launched and the task waits

### Scenario: the console offers every configured model for a pinned wait

- **Given** a `frontend` task about to enter `implement` with no implement pick; the gate
  denies `claude-fable-5-1` and `claude-opus-5` and admits every other model
- **When** the operator opens the task
- **Then** its wait reason offers `claude-opus-5`, `claude-sonnet-5`, `openai/gpt-astra` and
  `openai/gpt-sol` besides the denied `claude-fable-5-1`, each with its usage verdict

### Scenario: an unpinned wait offers its own list as today

- **Given** a `standard` task about to enter `implement` with no implement pick, and the gate
  denies `claude-sonnet-5` and `openai/gpt-sol`
- **When** the operator opens the task
- **Then** its wait reason offers `claude-sonnet-5` and `openai/gpt-sol` and no other model

### Scenario: an override of a pin covers one ticket

- **Given** an `architecture` task with 4 tickets; tickets 02 and 04 have the ticket track
  `frontend`; ticket 01 is done; the gate denies `claude-fable-5-1` and `claude-opus-5` and
  admits every other model; the operator chooses `openai/gpt-astra` for the waiting task
- **When** the dispatcher passes run
- **Then** ticket 02 runs on `openai/gpt-astra`, ticket 03 runs on `openai/gpt-astra@medium`
  from the `architecture` list, and ticket 04 is not started: the task waits again

### Scenario: with a pick, only that provider's models are offered

- **Given** an `architecture` task whose ticket 02, with the ticket track `frontend`, is in
  progress on `claude-opus-5@medium` and parked, and the gate now denies `claude-opus-5`
- **When** the operator opens the task
- **Then** its wait reason offers `claude-fable-5-1` and `claude-sonnet-5` and no openai model,
  and a request to run it with `openai/gpt-astra` is refused

## Out of scope

- The usage gate, the 80% session threshold, the allowance and the headroom do not change.
- No fallback to a model outside a pinned track's list, and no pin per stage.
- No ticket track for spec, plan or review, and no ticket track that names an unpinned track.
- No pin control in the console and no pin for `models.triage`.
- The box's live `targets.yaml` and the infra seed config are not changed by this spec.
