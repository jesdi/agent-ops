# A task's PR can target a feat/<name> base branch — Spec

Terms:

- The **base line** is the first line of the issue body that matches `Base: <branch>`
  (leading and trailing spaces ignored). A body with no such line has no base line.
- A task's **base** is the branch its work starts from and its PR merges into. It is the
  branch named by the base line, or `main` when the issue has none.
- A **valid branch name** is one `git check-ref-format --branch` accepts and that holds only
  letters, digits and `/ . _ -`.

Task, repository, session, stage and operator are as in `CONTEXT.md`.

Unless a scenario says otherwise, the repository is `jesdi/shop` with default branch `main`,
the issue is #42 "Swap checkout to the new cart", and origin has the branches `main` and
`feat/new-cart`.

## Requirements

1. **The base comes from the issue at claim.** When the box claims an issue, the task's base is
   the branch its base line names, or `main` when the body has no base line. The base is stored
   on the task and does not change for the life of the task, whatever later happens to the
   issue body. A task saved before this change has the base `main`.
2. **A base must be a valid branch name.** A base line whose value is not a valid branch name
   is treated like a base that is not on origin (requirement 3). Nothing from the value reaches
   a command or a prompt.
3. **A task does not start on a missing base.** When the base is not a branch on origin, the
   box does not claim the issue: it creates no worktree and no task, starts no session, files no
   failure issue and leaves the issue in Ready on the board. The operator gets one ping naming
   the issue and the missing branch; the box pings again only when the issue names a different
   base. Every pass checks the issue again, and the box claims it as soon as the branch exists.
4. **The task branch starts from the base.** The task branch `agent/task-<issue>` is created
   from the tip of `origin/<base>`.
5. **Every stage works against the base.** Wherever a stage prompt names the branch the work is
   compared with or rebased onto, it names `origin/<base>`: the review diff, the docs check of
   the review stage, the gate's rebase before the PR, and the rebase that fixes a conflict in
   address-review.
6. **The PR targets the base.** The review stage opens the PR with the base as its base branch.
   The PR body still starts with `Closes #<issue>`.
7. **A merged task closes its issue.** When the box sees a task's PR merged, the issue is
   closed as completed if it is still open, whatever the base. An issue already closed is left
   as it is.
8. **No base line, no change.** An issue without a base line runs exactly as today: branch from
   `origin/main`, every comparison and rebase against `origin/main`, PR into `main`.

## Scenarios

### Scenario: base line names the integration branch

- **Given** issue #42's body has the line `Base: feat/new-cart`
- **When** the box claims #42
- **Then** the task's base is `feat/new-cart`
- **And** `agent/task-42` starts at the tip of `origin/feat/new-cart`, not of `origin/main`

### Scenario: no base line

- **Given** issue #42's body has no line starting with `Base:`
- **When** the box claims #42
- **Then** the task's base is `main` and `agent/task-42` starts at the tip of `origin/main`

### Scenario: base line among other lines, with spaces

- **Given** issue #42's body is a paragraph, then `Blocked by: #40`, then `  Base: feat/new-cart  `
- **When** the box claims #42
- **Then** the task's base is `feat/new-cart`

### Scenario: two base lines

- **Given** issue #42's body has `Base: feat/new-cart` and, further down, `Base: feat/old-cart`
- **When** the box claims #42
- **Then** the task's base is `feat/new-cart`

### Scenario: explicit main

- **Given** issue #42's body has the line `Base: main`
- **When** the box claims #42
- **Then** the task runs exactly as an issue with no base line

### Scenario: base edited after claim

- **Given** the task for #42 was claimed with base `feat/new-cart` and is in the implement stage
- **When** the operator edits the body to `Base: main`
- **Then** the task's base is still `feat/new-cart`, its review diffs against
  `origin/feat/new-cart` and its PR targets `feat/new-cart`

### Scenario: task saved before this change

- **Given** a task for #17 saved before this change, with no base recorded, in the review stage
- **When** the box resumes it
- **Then** its review diffs against `origin/main` and its PR targets `main`

### Scenario: base not on origin

- **Given** issue #42's body has `Base: feat/new-cart` and origin has no branch `feat/new-cart`
- **When** the box would claim #42
- **Then** no worktree `task-42` and no task for #42 exist, and no session runs for #42
- **And** #42 is still in Ready on the board and no failure issue is filed
- **And** the operator gets one ping saying #42 waits for the branch `feat/new-cart`

### Scenario: missing base on later passes

- **Given** the box already pinged that #42 waits for `feat/new-cart`, which is still not on
  origin
- **When** the next 3 passes run
- **Then** #42 is still not claimed and the operator gets no further ping

### Scenario: base line changed to another missing branch

- **Given** the box already pinged that #42 waits for `feat/new-cart`
- **When** the operator edits the base line to `Base: feat/cart-v2`, which is not on origin
  either, and the next pass runs
- **Then** the operator gets one ping saying #42 waits for the branch `feat/cart-v2`

### Scenario: base pushed later

- **Given** #42 did not start because `feat/new-cart` was not on origin
- **When** the operator pushes `feat/new-cart` and the next pass runs
- **Then** the box claims #42 with base `feat/new-cart`

### Scenario: a missing base does not hold up other issues

- **Given** #42 waits for `feat/new-cart`, issue #44 has no base line, and the box has room for
  one task
- **When** the next pass runs
- **Then** the box claims #44

### Scenario: base value is not a valid branch name

- **Given** issue #42's body has the line `Base: feat/new-cart; rm -rf ~`
- **When** the box would claim #42
- **Then** the issue is handled as a base that is not on origin: not claimed, still in Ready,
  and one ping says the base line of #42 names no valid branch
- **And** no command and no prompt contains the text `rm -rf`

### Scenario: base value at the boundary of the allowed characters

- **Given** issue #42's body has `Base: feat/new-cart_v2.1` and that branch is on origin
- **When** the box claims #42
- **Then** the task's base is `feat/new-cart_v2.1`

### Scenario: review diffs against the base

- **Given** the task for #42 has base `feat/new-cart`, and `feat/new-cart` is 3 commits ahead of
  `main` with changes the task did not make
- **When** the review stage runs
- **Then** its prompt has `review-diff` run on `origin/feat/new-cart...HEAD` and the docs check
  on `origin/feat/new-cart...HEAD`
- **And** no occurrence of `origin/main` is left in the prompt

### Scenario: gate rebases onto the base

- **Given** the task for #42 has base `feat/new-cart`
- **When** the review stage reaches the gate
- **Then** its prompt rebases with `git rebase origin/feat/new-cart`

### Scenario: PR opened into the base

- **Given** the task for #42 has base `feat/new-cart`
- **When** the review stage opens the PR
- **Then** its prompt runs `gh pr create` with `--base feat/new-cart`
- **And** the PR body's first line is `Closes #42`

### Scenario: conflict fixed against the base

- **Given** the task for #42 has base `feat/new-cart` and its PR reports a conflict
- **When** address-review runs for the conflict
- **Then** its prompt rebases with `git rebase origin/feat/new-cart`

### Scenario: merge into the integration branch closes the issue

- **Given** the task for #42 has base `feat/new-cart` and its PR #57 into `feat/new-cart` is
  merged, so GitHub leaves #42 open
- **When** the box sees #57 merged
- **Then** #42 is closed as completed and the task is done

### Scenario: merge into main, issue already closed

- **Given** the task for #43 has base `main`, its PR #58 is merged and GitHub closed #43 through
  `Closes #43`
- **When** the box sees #58 merged
- **Then** the box does not close or comment on #43 again and the task is done

### Scenario: closing the issue fails

- **Given** the task for #42 has base `feat/new-cart`, PR #57 is merged and the call that closes
  #42 fails
- **When** the box sees #57 merged
- **Then** the task is not marked done in that pass, and the next pass closes #42 and finishes
  the task

## Out of scope

The dispatcher never creates, rebases or merges the integration branch, never reads the
repository's default branch from GitHub, and never changes a claimed task's base. A base
branch rewritten under a task in flight goes through the existing conflict path. The full
list is in the description of the pull request that implemented this spec, and the rules
that outlive it are in ADR 0006.
