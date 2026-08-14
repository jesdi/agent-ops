# Runbook: stale worktree sweep

Why task worktrees pile up, what `provision/sweep-worktrees.sh` will and
will not delete, and how to handle the cases it deliberately refuses.
Written after five orphaned worktrees (~3.8 GB, tasks 162/183/184/192/200)
accumulated on a 38 GB box alongside a zombie tmux session from 2026-08-02.

## Why they pile up

The dispatcher removes a worktree itself when a PR merges —
`workspace.remove_workspace`, called from the done path in `main.py`.
That is the primary mechanism and it stays primary. Two cases never reach
it:

- **Orphans.** The task's `<state>/task-N.json` is gone, so the dispatcher
  no longer knows the task exists. Four of the five leftovers were this.
- **Stale `failed`.** The task sits at `stage=failed` while its PR is
  merged and its issue closed — a state-machine desync, not a real
  failure. `workspace.py` preserves failed worktrees for autopsy by
  design, so nothing ever collects them. Tasks 148 and 183 were this.

Each leftover is ~770 MB, almost all of it `.venv` and `.pnpm-store`.

## What the sweeper deletes

Dispatcher state is **not** consulted for the delete decision — it is the
thing that desyncs. Git and GitHub are ground truth. All seven must hold:

1. registered in `git worktree list` for its target's clone
2. every commit is an ancestor of `origin/main` (after a fetch)
3. no uncommitted tracked changes — `.my-skills.json` excepted, see below
4. no unpushed commits versus its remote branch
5. its GitHub issue is CLOSED
6. no live `task-N` tmux session and no running `task-N` container
7. no `<state>/attached-N` marker (you are not attached to it)

`--sweep` adds an eighth: last commit **and** worktree mtime both older
than `AGENT_OPS_WORKTREE_STALE_DAYS` (default 7). Naming a single target
skips that one — "is this safe to remove" is a different question from
"has this been abandoned".

Failing to reach GitHub reads as "not closed", so an offline box sweeps
nothing rather than guessing.

> **`.my-skills.json` is always modified.** `claude-home-sync` rewrites it
> in every worktree, so all 20+ report exactly one dirty file. It is
> provisioning drift, never work. Ignoring it is the difference between
> this script finding candidates and finding none — do not "fix" that
> exclusion without checking what the dirty file actually is.

On removal, in order: snapshot `.agent/` to
`<state>/autopsy/task-N-agent/`, remove the worktree, delete the local
branch, delete the remote branch if it survives, drop an orphaned
`<state>/task-N.json`, append a `worktree_swept` event.

The `.agent/` snapshot is what keeps the `workspace.py` autopsy rule
honest: the plan, stage signal and model log survive the deletion, and
those are the only irreplaceable bytes in a ~770 MB tree.

## Running it

    provision/sweep-worktrees.sh --sweep --dry-run   # report, change nothing
    provision/sweep-worktrees.sh --sweep             # what the timer runs
    provision/sweep-worktrees.sh 162                 # one worktree, no age gate
    provision/sweep-worktrees.sh /path/to/task-162   # same, by path

Exit codes: `0` removed or nothing to do, `3` a named target was refused,
`1` error (bad arguments, unknown task, lock contention).

`agent-ops-sweep.timer` fires it daily at 03:00, `Persistent=true`.

It takes `<state>/convergence.lock` — the same lock as `update.sh` and the
dispatcher pass — so it can never evaluate a worktree that a pass is
midway through creating and delete it for having no commits.

## Refusals worth acting on

- **`not registered as a worktree`** — a directory under `.worktrees/`
  that git does not know about, i.e. wreckage from a `git worktree add`
  killed mid-run (`_worktree_health_issue` in `workspace.py` covers the
  same failure). `.worktrees/task-197` is one: 16 KB, `.agent/` only, no
  `.git`. The sweeper will never touch these — inspect and `rm -rf` by
  hand once you know what it is.
- **`N commit(s) not in origin/main`** on a task you believe is finished —
  the PR was probably squash-merged, or the branch was re-pushed after
  the merge. Confirm with `gh pr list --head <branch> --state all` before
  deleting anything manually.
- **`operator attached`** — clear `<state>/attached-N` only if you are
  certain no one is in that session.

## Adding it to a box provisioned before 2026-08-14

`update.sh` syncs unit *files* but only `try-restart`s them; it never
enables a timer that did not exist before. `bootstrap.sh` handles fresh
boxes. An existing box needs the enable once, by hand:

    systemctl --user enable --now agent-ops-sweep.timer
