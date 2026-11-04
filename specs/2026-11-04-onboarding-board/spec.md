# Board preparation during onboarding

Sources: https://github.com/jesdi/agent-ops/issues/230 (D1–D13, amended by #212 A6), step order in https://github.com/jesdi/agent-ops/issues/210, scenarios S06–S11, S52, S57 of `specs/project-onboarding/spec.md`. Implements onboarding increment 7 (R4–R6). Builds on increment 6 (inspection).

Outcome: open issues are imported once, future ones keep arriving, reruns don't duplicate.

## Behavior

**Board choice.** The proposal's board is one of: the board in a repository's `.backlog/project-meta.json` (preselected by inspection), another board of the owner typed as `owner/number`, or a new board. A new board is owned by the owner of the project's first repository and titled `<project> Backlog`. Config stores only `board: {owner, number}`; field and option ids are always read from the live board.

**Checks before confirm.** Resolve (without inspection) and confirm read the chosen existing board and refuse it when:
- a required field exists with the wrong type: `Status` and `Area` must be single select, `Impact`, `Effort`, `Score` and `Boost` must be numbers. The block names the field.
- a Status option differs from a required one only in case (GitHub's default `In Progress`). The block asks for a rename instead of adding a near-twin.
- a card is an issue or PR from a repository outside the project, open or closed. The block lists up to 10 such cards and asks for another board. Draft cards are fine. A card the board token can't read (redacted private content) that doesn't join by title to a member issue is reported as unreadable, not foreign.

The proposal lists the planned additions (missing fields, missing Status options, missing labels per repository) so the operator confirms them.

**Board step.** A new `board` step runs after confirm. It recomputes the additions from the live board and applies only what is still missing; a new type clash or foreign card blocks with a link and does not go back to confirm.
- New board: created once, with exactly the five Status options in place of GitHub's defaults. The record keeps its number. A crash that loses the save is recovered by finding the board by its title; two boards with that title block. A found board with no cards gets the five options too, since no value can be lost.
- Structure: a missing number field is created. Missing Status options (`Inbox`, `Ready`, `In progress`, `Done`, `Wont do`) are appended, resending every existing option with its `id` so card values survive (#213). A missing `Area` is created with `feature, bug, infra, docs, research`; an existing `Area` keeps its options. Extra fields, options and views are left alone.
- Labels: one label set, defined once in agent-ops code: the backlog taxonomy (`inbox`; `bug`, `enhancement`, `documentation`, `question`; `frontend`, `backend`, `infra`, `ci`, `security`, `performance`, `testing`, `dependencies`; `auto`, `human-required`, `spec-ready`, with the backlog skill's colors and descriptions) plus `track:<name>` for every track under `models.tracks`. Missing labels are created in every repository of the project. Existing labels are not changed, nothing is renamed or deleted, no issue's labels change.
- Linking: the board is linked to every repository owned by the board's owner; others are skipped with a note.
- Import: every open issue of every repository is added by node id. An issue already on the board keeps its card and values. Every card of an open member issue that has no Status gets `Inbox`, including one an earlier, interrupted add left behind; a card with a Status keeps it, so a rerun never resets triage. `Inbox` is resolved before any write, so a board without it gets no card at all. Closed issues and PRs are not imported and no labels are added, so import never marks an issue `auto`. The same issue number in two repositories gives two cards (S57).
- Verified when a fresh read shows the full structure, every label on every repository, and a card for every open issue counted at import.

Board work uses `GH_PROJECT_TOKEN`; labels and issue reads use the box's repository credentials (the App token replaces them in increment 8). No new credential.

**Future issues.** Every dispatcher pass, for every project (paused included), diffs the open issues ranking already reads against the board's items. Each open issue with no card is added with `Inbox`, and the pass logs what it added. The same pass then creates missing labels, so a track added later reaches every project; a repository that refuses a label can't hold the cards back. Onboarding's board step and this pass share one survey of the board and one sync. Boards with GitHub's Auto-add workflow keep it; reconcile then finds nothing to add.

**Compatibility.** Records completed before this version stay complete. Open records gain the `board` step in place, before `config-pr`.

## Verification

Test interfaces: onboarding engine runs against a stateful fake GitHub (board fields, options, items, labels, issues) behind the `sh` seam, so reruns see earlier writes; dispatcher passes with the same fake at the gh boundary; CLI `--board new`; console proposal form and browser flow on desktop and phone. Cover: new board created once across reruns and crashes, option ids resent, type clash, foreign and unreadable cards, labels created missing-only, import idempotence and Inbox only on new cards, two repositories with the same issue number, reconcile adding a later issue and a later track label, and existing records.
