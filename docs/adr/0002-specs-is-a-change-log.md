# 0002. `specs/` is a change log; `main` keeps only `spec.md`

Date: 2026-10-07

## Context

A dated design document that stays on `main` is read by later sessions as if
it described the present state. The proposal and the design of a change are
scoped to that change; the behaviour spec is the part worth keeping.

## Decision

- A change's spec folder is `specs/<YYYY-MM-DD>-<topic>/`. During the task it
  holds `proposal.md`, `spec.md` and `design.md`.
- Before the pull request is opened, the Why, Goal and Non-goals of
  `proposal.md`, the Decisions of `design.md` and the open rulings go into
  the pull request description; each decision that constrains later changes
  becomes an ADR under `docs/adr/`; each new term goes into `CONTEXT.md`;
  then `proposal.md` and `design.md` are removed from the branch.
- After the merge the folder on `main` holds `spec.md` and nothing else. No
  review copy of questions and answers is committed.
- Every stage prompt says that `specs/` is a change log, that the code and
  `CONTEXT.md` are the present state, and that the code wins where they
  disagree.
- The description is written to a file before anything is removed, so a
  session that stops part way loses no content.

## Consequences

- There is no living spec per capability. The acceptance tests are the
  present-state spec.
- A decision that must outlive its change has to be written as an ADR at
  review time, or it survives only in a pull request description.
- This repository follows the same rule as the target repositories.
