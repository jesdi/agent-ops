# A task's PR can target a feat/<name> base branch — Design

## Decisions

- **The task stores its base as a plain branch name, `main` by default.** A task saved before
  this change loads with `main` and needs no migration; the cost is that "no base line" and
  `Base: main` cannot be told apart later, which nothing needs.
- **One pure parser turns an issue body into a base or a rejection.** It returns the branch of
  the first base line, `main` with no base line, or "invalid" with the raw value kept out of
  every command and prompt. Parsing and validation are tested without git or GitHub; the cost
  is one more small function beside the `Blocked by:` reading.
- **The body is read only for the candidate about to be claimed.** One `gh` call per claim, not
  one per ranked candidate. A body edited between ranking and claim is read at its claim-time
  state, which is what requirement 1 means by "at claim".
- **The base check lives where the worktree is made.** The workspace step checks for
  `origin/<base>` after its own fetch and right before `git worktree add`, and reports a missing
  base as its own error, distinct from a provisioning failure. The claim loop handles that
  error as a wait (no task, no failure issue, no quarantine). This closes the gap between a
  separate check and the add (red-team 4); the cost is that the workspace step now knows about
  bases.
- **A missing base does not take the repository out of the pass.** Unlike a provisioning
  failure, the claim loop moves on to the repository's next candidate in the same pass.
- **The wait is remembered per (repository, issue), holding the branch last pinged for.** A new
  ping goes out only when the branch differs; the record is deleted when the issue is claimed.
  An invalid base line is remembered under its raw value, so each different invalid value pings
  once. Cost: one small file per waiting issue under the state directory, which stays until the
  issue is claimed or the operator removes it.
- **The ping has no task behind it.** It goes through the notifier with the repository, issue,
  title and branch, not through the per-task notify helper.
- **Every stage prompt gets `$base`.** Prompt rendering is strict, so the variable is in every
  stage's context, not only review and address-review. The four `origin/main` references and
  `gh pr create` use it; nothing else in the prompts changes.
- **The issue is closed when the box finishes a merged task, whatever the base.** It is a
  raising step placed before teardown and before the state flip, so a failed close leaves the
  task as merged-but-not-done and the next pass retries, like the board update today. An issue
  that is already closed is left as it is (no second close, no comment).
- **A surviving task branch keeps today's path.** When `agent/task-<issue>` survived a removed
  worktree, the box attaches to it as today without checking it against the base (red-team 3).
  It happens only when a canceled task is reopened, and the review diff shows the mismatch.
- **The dispatcher does nothing when the base is merged, deleted or rewritten under a task in
  flight.** Merging the final PR while increment PRs still target the base is guarded where
  that merge happens, in babysit-pr (jesdi/general-skills#153), not here (red-team 1). A
  rebase of the base between merges goes through the existing conflict path (red-team 2).
- **A base line may name any valid branch on origin.** No `feat/*` restriction: only someone
  with push access can make a base exist, and the base is fixed at claim (red-team 5).

## Data model

No database. The task record is a JSON file per task; the wait record is new.

| Table | Field | Constraint | Why |
|---|---|---|---|
| task record (`TaskState`) | `base: str` | never empty; defaults to `main` on load; a valid branch name; set once at claim, never written again | requirement 1; a legacy file loads as `main` (requirement 8) |
| base wait record (new, per repository and issue) | `branch: str` | the branch (or raw invalid value) last pinged for; at most one record per (repository, issue) | requirement 3: one ping per issue and branch |
| base wait record | lifetime | deleted when that issue is claimed | a later wait for the same branch pings again |

## Identities

None. No key is widened: the task keeps its (repository, issue) identity and branch name
`agent/task-<issue>`.

## Seams

- `dispatcher.workspace.create_workspace(target, issue, base)`: existing, gains `base`. Run
  against a real git origin and clone in a temporary directory: where the task branch starts,
  and the distinct missing-base error.
- `dispatcher.main.run_pass(cfg, deps)`: existing. One dispatcher pass against a fake GitHub,
  fake notifier and fake session launcher, with the workspace step faked as the current tests
  do. Drives the claim (base read and stored, waits and pings), the launched stage prompts,
  and the merge finish.
