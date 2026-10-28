# A task's PR can target a feat/<name> base branch

## Why

Every task is cut from `origin/main`, reviewed against `origin/main`, rebased onto `origin/main`
and opened as a PR into the repository's default branch. agents-wayfinder hands off increments
that cannot land hidden on main: an all-at-once cut-over goes to a `feat/<name>` integration
branch instead. Each of its increments is a build issue whose PR targets that branch, and one
final PR merges the branch into main. The box cannot run those issues today: the task would
branch from main, review a diff against main and open its PR into main. When such a PR does
merge into `feat/<name>`, `Closes #N` does nothing (GitHub closes an issue only on a merge
into the default branch), so the issue stays open after its task is done.

## For whom

The operator, who files a wayfinder increment as a build issue on a project board and expects
the box to run it like any other task, with its PR into the integration branch.

## Goal

A build issue can name its base branch with a `Base: <branch>` line; its task branches from,
reviews against, rebases onto and opens its PR into that branch, and its issue is closed when
the PR merges. An issue without the line runs exactly as today, against `main`.

## Non-goals

- The dispatcher does not create, rebase or merge the integration branch. Rebasing
  `feat/<name>` onto main after each merge and opening the final PR into main stay with
  wayfinder or the operator.
- No base other than through the issue body: no label, board field, project or repository
  setting, and no console control to change a task's base.
- No change of base for a task already claimed. Editing the `Base:` line later has no effect
  on that task.
- No special handling when the base branch is rewritten (rebased, force-pushed) under a task
  in flight: the existing conflict path of address-review handles the PR's conflict.
- No reading of the repository's default branch from GitHub. The default base stays `main`,
  as today.
- No change to agents-wayfinder in this repository. A separate PR in general-skills makes it
  write the `Base: feat/<name>` line in the build issues it hands off.
- No console display of the base.
