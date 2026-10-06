# Openspec pipeline: one review of spec and plan, one implement session — Spec

Terms:

- The **spec folder** of a task is `specs/<YYYY-MM-DD>-<topic>/` in the task's worktree, dated
  with the day the spec session starts. It holds `proposal.md`, `spec.md` and `design.md`.
- **Stage 1** and **stage 2** are the two stages of the `to-openspec` skill. Stage 1 writes
  `proposal.md` and `spec.md`. Stage 2 runs `red-team-data-model`, writes `design.md` and writes
  the ticket files under `.agent/tickets/`.
- The **plan review gate** is the one point where a task waits for the operator before a pull
  request exists. It comes after stage 2.
- An **open question** is what stage 2 reports at its end: a red-team finding the design leaves
  unresolved, a choice between two behaviours that differ for the user or the business, or an
  input the design assumes and no source provides.
- A **gate-free track** is a track whose `targets.yaml` entry carries `plan_review: false`. A
  track without the key has the gate.
- The **ledger** is `.agent/ledger.md` in the task's worktree: the rulings of the implement
  session and the state of every ticket.

Unless a scenario says otherwise, the task is issue #412 "Export a portfolio as CSV" in
`jesdi/portfolio_eval`, on branch `agent/412-csv-export`, started on 2026-10-12, with the spec
folder `specs/2026-10-12-csv-export/`, on the `standard` track, and the tracks are `trivial`
(`plan_review: false`), `standard` and `security`.

## Requirements

1. **The spec session writes stage 1 and does not wait for approval.** It ends with
   `proposal.md` and `spec.md` committed and pushed in the spec folder, and with a track chosen.
   The plan session then starts with no action from the operator.
2. **The questionnaire is the only human step of the spec session.** A task with neither the
   `bug` nor the `spec-ready` label raises one questionnaire when a decision is open, parks, and
   writes stage 1 from the issue and the answers.
3. **A `spec-ready` task writes stage 1 from the issue body.** The spec session raises no
   questionnaire, copies the settled decisions without change, and records in `spec.md` what
   moved in the code underneath them and how the design absorbs it. Only a contradiction
   between a settled decision and the code raises a questionnaire.
4. **A `bug` task reproduces first, then follows the same pipeline.** The spec session commits
   a failing end-to-end test that reproduces the bug, then writes stage 1 from the diagnosis:
   the root cause under **Why** in `proposal.md`, the reproduction as the first scenario of
   `spec.md`. It writes no separate diagnosis document.
5. **The plan session checks stage 1 before it builds on it.** It runs in a session that did
   not write stage 1. It corrects `spec.md` where a requirement has no source in the issue, the
   answers or the code, where a requirement has no scenario, where an invariant lacks its
   boundary scenario or its violation scenario, and where a price, policy, deadline or
   permission was invented. It reports every correction at the gate.
6. **The plan session writes stage 2.** It ends with `design.md` committed and pushed in the
   spec folder and with a valid ticket set under `.agent/tickets/`, under the same mechanical
   rules as today (numbers contiguous from 01, each file with what to build, blocked-by and an
   unchecked criterion).
7. **The plan review gate is mandatory.** After stage 2 the task waits for the operator, who is
   shown the spec folder, the ticket list, the open questions and the corrections of
   requirement 5. The spec folder is linked on GitHub; the ticket list, the open questions and
   the corrections are one summary the console shows with the task. Implement starts only on
   the operator's explicit approval. Requirement 9 is the one exception.
8. **Feedback at the gate is applied by the plan session.** It rewrites what depends on the
   change (a changed requirement: `design.md` and the tickets; a changed decision: the
   tickets), commits, pushes and waits again. The task never returns to the spec stage. An
   approval may name a track, which then runs implement and review.
9. **The gate is skipped only when three conditions hold together:** the task's track is
   gate-free, the spec session raised no questionnaire, and stage 2 reported no open question.
   Then implement starts with no action from the operator. If any one fails, requirement 7
   applies.
10. **The gate waits as the spec gate waits today.** The session stays alive for
    `spec_review_grace_minutes` (15 by default); after that the task parks and releases its
    slot, and the operator's reply resumes it.
11. **One session implements every ticket.** The implement stage is a single session that runs
    `implement-spec` over the whole ticket set. The dispatcher starts no session per ticket.
12. **The implement session creates no pull request.** It works on the task branch, which
    already exists. The review stage alone opens the pull request.
13. **The implement session keeps the ledger in the worktree and reports progress.** After each
    ticket is merged into the task branch it reports how many of the tickets are merged, and
    the console shows that count.
14. **A session that cannot dispatch isolated subagents does not implement.** It reports that it
    is blocked, and the task parks with that reason.
15. **A stopped implement session is continued, not started over.** When the session stops
    before every ticket is merged, the task stays resumable as today: parked when the session
    is alive and silent (a quota error), failed and resumable when the session died. The
    session that continues, the same one or a fresh one, works from the ledger, and the tickets
    already merged stay on the branch.
16. **Implement is admitted once.** The usage gate is asked when the implement session starts
    and not again for that session. The gate's numbers are unchanged.
17. **The review stage is independent and unchanged in method.** It runs in a fresh session, on
    the other provider when one is admitted, reviews the branch from primary sources, and does
    not read the ledger before its own review is finished.
18. **The review stage moves the slice-scoped content off the branch.** Before it opens the pull
    request it writes into the pull request description the Why, Goal and Non-goals of
    `proposal.md`, the Decisions of `design.md` and the open rulings of the ledger; adds to
    `docs/adr/` each decision that constrains later changes and to `CONTEXT.md` each new term;
    and removes `proposal.md` and `design.md` from the branch.
19. **`main` keeps only `spec.md` of a change.** After the merge, the spec folder on `main`
    holds `spec.md` and nothing else. No review copy of questions and answers is committed.
20. **Sessions are told what `specs/` is.** Every stage prompt states that `specs/` is a change
    log, that the code and `CONTEXT.md` are the present state, and that the code wins where the
    two disagree.
21. **A task of the old flow in the spec stage restarts with a fresh spec session.** A one-time
    migration, run at the deploy, makes every task that is in the spec stage or at the old spec
    review gate start a new spec session that does not resume the old one. What the task
    already has is input: an unanswered questionnaire is asked again as it is, an answered one
    counts as the settled decisions, and an old design file is handled as a `spec-ready` body
    and removed from the branch.
22. **Tasks with an open pull request are not touched** by the migration or by this change.

## Scenarios

### Scenario: the spec session ends without a review gate

- **Given** issue #412 with no `bug` or `spec-ready` label, and the operator has answered its
  questionnaire of 4 questions
- **When** the spec session commits and pushes `specs/2026-10-12-csv-export/proposal.md` and
  `spec.md` and reports the track `standard`
- **Then** the plan session starts in the same pass that sees the report, the operator receives
  no request to review, and the task is never in a state that waits for a spec approval

### Scenario: a spec session that names no configured track is bounced

- **Given** the spec session reports stage 1 done with the track `fast`, which `targets.yaml`
  does not define
- **When** the dispatcher reads the report
- **Then** the session is resumed once with the list `security, standard, trivial`, and the plan
  session does not start until a configured track is named

### Scenario: an open decision raises one questionnaire

- **Given** issue #412 with no label, and the issue does not say which columns the CSV has
- **When** the spec session finishes exploring
- **Then** it raises one questionnaire that holds every open decision, the task parks, and no
  file exists yet in `specs/2026-10-12-csv-export/`

### Scenario: a spec-ready issue is not interviewed

- **Given** issue #412 labelled `spec-ready`, whose body settles "the export has the columns
  ticker, quantity, price", and the code still has those three fields
- **When** the spec session runs
- **Then** it raises no questionnaire, `spec.md` carries that decision word for word, and the
  plan session starts

### Scenario: a spec-ready decision the code contradicts raises a questionnaire

- **Given** issue #412 labelled `spec-ready`, whose body settles "the export reads the `price`
  field", and the code has replaced `price` with `price_minor` and `currency`
- **When** the spec session reconciles the body with the code
- **Then** it raises a questionnaire about that one decision, with its recommended answer, and
  asks nothing about the other settled decisions

### Scenario: a bug is reproduced before its spec is written

- **Given** issue #430 "CSV export drops the last row" labelled `bug`
- **When** the spec session ends
- **Then** the branch holds a commit with an end-to-end test that fails because the last row is
  missing, `proposal.md` states the root cause under **Why**, the first scenario of `spec.md`
  is the reproduction with the expected last row present, and no diagnosis document exists
  under `docs/specs/`

### Scenario: the plan session corrects a requirement with no source

- **Given** `spec.md` from stage 1 has 6 requirements, and requirement 5 says "an export is
  limited to 10 000 rows", a limit that neither the issue, the answers nor the code mention
- **When** the plan session checks stage 1
- **Then** requirement 5 is no longer in `spec.md` as a settled rule, the limit appears as an
  open question, and the gate shows the correction

### Scenario: an invariant without its violation scenario is completed

- **Given** `spec.md` has the requirement "only the owner of a portfolio may export it" with a
  scenario for the owner and none for another user
- **When** the plan session checks stage 1
- **Then** `spec.md` has a scenario in which a signed-in user who is not the owner is refused,
  and the gate shows the correction

### Scenario: the task waits at the plan review gate

- **Given** the plan session has written `design.md` and 4 valid tickets, with 0 open questions,
  on the `standard` track
- **When** it reports that the plan is ready for review
- **Then** the task waits, the operator is notified with a link to
  `specs/2026-10-12-csv-export/` on GitHub, the console shows with the task one summary that
  lists the 4 tickets (number, title, blocked by, seam), "no open questions" and the
  corrections, and no implement session starts

### Scenario: approval starts implement

- **Given** the task waits at the plan review gate
- **When** the operator replies "approved"
- **Then** one implement session starts on the track `standard`

### Scenario: approval names another track

- **Given** the task waits at the plan review gate on the `standard` track
- **When** the operator replies "approved, but run it as security"
- **Then** the implement session and the review session run on the `security` track's entries

### Scenario: feedback on a requirement rewrites the design and the tickets

- **Given** the task waits at the gate with 4 tickets, and the operator replies "drop the
  currency column, requirement 3"
- **When** the plan session applies the reply
- **Then** requirement 3 is changed in `spec.md`, `design.md` and the tickets no longer mention
  the currency column, the changes are pushed, the task waits at the gate again, and the task's
  stage was never the spec stage in between

### Scenario: an invalid ticket set does not reach the gate

- **Given** stage 2 wrote tickets `01`, `02` and `04`
- **When** the plan session reports that the plan is ready
- **Then** the session is resumed once with the reason "ticket numbers must be contiguous from
  01", and the operator is not asked to review

### Scenario: a trivial task with nothing open skips the gate

- **Given** issue #415 "Fix a typo in the export button label" on the `trivial` track, the spec
  session raised no questionnaire, and stage 2 reported 0 open questions and 1 ticket
- **When** the plan session reports that the plan is ready
- **Then** the implement session starts with no reply from the operator, and the spec folder,
  `design.md` and the ticket exist as for any other task

### Scenario: a trivial task with one open question waits

- **Given** the same task, but stage 2 reported 1 open question
- **When** the plan session reports that the plan is ready
- **Then** the task waits at the plan review gate

### Scenario: a trivial task that needed a questionnaire waits

- **Given** issue #415 on the `trivial` track, the spec session raised a questionnaire of 1
  question that the operator answered, and stage 2 reported 0 open questions
- **When** the plan session reports that the plan is ready
- **Then** the task waits at the plan review gate

### Scenario: a standard task with nothing open still waits

- **Given** issue #412 on the `standard` track, no questionnaire was raised, and stage 2
  reported 0 open questions
- **When** the plan session reports that the plan is ready
- **Then** the task waits at the plan review gate

### Scenario: the gate releases its slot after the grace time

- **Given** `spec_review_grace_minutes` is 15 and the task has waited at the plan review gate
  for 16 minutes with no reply
- **When** the dispatcher runs a pass
- **Then** the task parks, its session ends, its slot is free for the next task, and a later
  "approved" from the operator starts implement

### Scenario: one implement session works every ticket

- **Given** an approved plan with 4 tickets
- **When** implement runs to its end
- **Then** exactly one implement session was started for the task, the 4 tickets are merged
  into `agent/412-csv-export`, and the review session starts

### Scenario: the console shows implement progress

- **Given** the implement session has merged tickets 01 and 02 of 4
- **When** the operator opens the task on the console
- **Then** the task shows "2/4 tickets merged"

### Scenario: implement opens no pull request

- **Given** the implement session has merged all 4 tickets
- **When** it reports that it is done
- **Then** `jesdi/portfolio_eval` has no pull request for `agent/412-csv-export`, and the first
  one is opened by the review session

### Scenario: a session without subagents parks the task

- **Given** the implement session runs on a runtime where it cannot dispatch an isolated
  subagent
- **When** it starts the first ticket
- **Then** it reports that it is blocked for that reason, the task parks, and no ticket is
  implemented by the main session itself

### Scenario: a quota error mid-implement parks and resumes

- **Given** the implement session has merged tickets 01 and 02 of 4, the ledger records ticket
  03 as "tests locked, implementer running", and the provider returns a quota error
- **When** the session stops, and the operator resumes the task 3 hours later
- **Then** the task was parked and not failed, tickets 01 and 02 are still on the branch, and
  the resumed session continues with ticket 03 without redoing 01 or 02

### Scenario: a fresh session after a crash continues from the ledger

- **Given** the implement session died after merging tickets 01 and 02 of 4, the task is failed
  and resumable, and the ledger records both merges
- **When** the operator resumes the task
- **Then** a new implement session starts in the same worktree, it does not redo tickets 01 and
  02, and it ends with all 4 tickets merged

### Scenario: implement is admitted once

- **Given** the usage gate admits the implement entry when the session starts, and 40 minutes
  later the same entry would be denied
- **When** the session moves from ticket 02 to ticket 03
- **Then** the session keeps running, and the denial applies to the next session the box wants
  to start

### Scenario: review runs on the other provider and opens the pull request

- **Given** the implement session ran on an `anthropic` entry and the review list has an
  admitted `openai` entry
- **When** the review stage starts
- **Then** the review session runs on the `openai` entry, and when it ends the pull request
  exists with "Closes #412"

### Scenario: the pull request carries what the removed files said

- **Given** `proposal.md` has a Goal and 3 Non-goals, `design.md` has 5 Decisions, and the
  ledger has 2 open rulings
- **When** the review session opens the pull request
- **Then** the description holds the Why, the Goal, the 3 Non-goals, the 5 Decisions and the 2
  rulings, and the branch has no `proposal.md` and no `design.md`

### Scenario: main keeps only spec.md

- **Given** the pull request for #412 is merged
- **When** `specs/2026-10-12-csv-export/` is listed on `main`
- **Then** it holds `spec.md` only, and `main` has no file under `docs/review/` from this task

### Scenario: a decision that constrains later work becomes an ADR

- **Given** `design.md` decides "every export is streamed, never built in memory", which later
  exports must follow
- **When** the review session prepares the pull request
- **Then** the branch has a new file under `docs/adr/` for that decision

### Scenario: a session is told that specs are history

- **Given** `main` of the target has `specs/2026-08-02-export-json/spec.md`, which says the
  export button is on the settings page, and the code has it on the portfolio page
- **When** a spec, plan, implement or review session starts for a later task
- **Then** its prompt says that `specs/` is a change log and that the code is the present
  state, so the session plans against the portfolio page

### Scenario: an old task with an unanswered questionnaire restarts

- **Given** before the deploy, task #370 is parked in the spec stage with
  `.agent/questionnaire.md` of 5 questions and no answer
- **When** the migration has run and the dispatcher next works the task
- **Then** a spec session starts that is not a resume of the old one, the operator is asked the
  same 5 questions, and after the answers stage 1 is written

### Scenario: an old task at the spec review gate restarts from its design

- **Given** before the deploy, task #384 waits at the old spec review gate with
  `docs/specs/2026-10-01-alerts-design.md` on its branch
- **When** the migration has run and the dispatcher next works the task
- **Then** a new spec session writes stage 1 from that design with no questionnaire, the old
  design file is removed from the branch, and the task continues to stage 2 and the plan review
  gate

### Scenario: a task with an open pull request is untouched

- **Given** before the deploy, task #391 is in `pr-open`
- **When** the migration has run
- **Then** the task is still in `pr-open` with the same pull request, and no session was started
  for it

## Out of scope

- The interactive review page, the answers file and the questionnaire page (part B). In this
  spec the operator reviews on GitHub and the console as they are, and replies with text.
- Tasks of the old flow that are in plan, implement or review at the deploy: the operator lets
  them finish before deploying. This spec gives them no behaviour.
- Verifying that Codex can dispatch isolated subagents (jesdi/agent-ops#161).
- The line in a target repository's own `CONTEXT.md` that says `specs/` is a change log, for
  sessions that do not run on the box. It is a change in that repository.
- The skill versions a target repository pins. This repository's pins are updated here; a
  target's are updated there.
- A usage check between tickets, a severity for red-team findings, a shorter pipeline for
  trivial work, and a living spec merged from every change.
