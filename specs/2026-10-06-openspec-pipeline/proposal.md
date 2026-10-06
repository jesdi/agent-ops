# Openspec pipeline: one review of spec and plan, one implement session

## Why

The box writes a spec with `to-spec`, stops for the operator's approval, writes tickets with
`to-tickets`, then runs one `tdd` session per ticket. Three things are wrong with that.

The operator approves the spec before the design and the tickets exist, so the only human gate
before the pull request covers the smaller half of the plan. A defect in the design or in the
ticket breakdown is first seen in the pull request, after the most expensive stage has run.

The implement sessions have no independent test author, no review per ticket and no parallel
work. `implement-spec` has all three, and the operator already uses it by hand on the Mac
together with `to-openspec`. The box and the Mac run different methods on the same kind of work.

The spec file the box writes stays on `main` for ever as a dated design document. A later
session that searches the repository reads old designs as if they were the present state.

## For whom

The operator of the box, who answers its questions, reviews its plans and merges its pull
requests. And the later sessions, on the box or on the Mac, that read the target repository.

## Goal

A task on the box goes from issue to pull request through `to-openspec` and `implement-spec`:
the operator reviews the spec, the design and the tickets together, once, after they are all
written; one session implements every ticket; and `main` keeps only the behaviour spec of each
change.

## Non-goals

- No review page. The interactive HTML review, the answers file written by the dispatcher and
  the questionnaire on the same mechanism are a separate spec (part B). Until then the operator
  reviews with what the console and GitHub show today and answers with text.
- No human gate after stage 1. A wrong stage 1 costs a second run of stage 2, nothing else.
- No shorter pipeline for trivial work. A trivial task runs every stage; only the gate is
  skipped.
- No stop on the severity of a red-team finding. `red-team-data-model` has no severity; the
  open questions list decides what the operator must look at.
- No usage check between tickets. The implement session is admitted once.
- No limit on which runtime runs implement. Codex is assumed able to dispatch isolated
  subagents with a named model; proving it on the box is jesdi/agent-ops#161.
- No support for two task formats in the dispatcher. Tasks from the old flow are restarted or
  drained, not carried.
- No living spec that merges every change into one document per capability. The locked
  acceptance tests are the present-state spec.
- No change to the skills in jesdi/general-skills. Only the versions this repository pins
  change.
- `to-spec` is not removed. The operator still uses it on the Mac to write a settled design
  into an issue body labelled `spec-ready`.
- No change to triage, to the usage gate's numbers, to the `pr-open` and address-review stages,
  or to the rule that review runs on the other provider when one is admitted.
