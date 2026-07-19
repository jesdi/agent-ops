---
name: branch-audit
description: >
  Audit local and remote git branches to see which are merged into main, which have
  unpushed commits, which have diverged, and which have stale worktrees. Use this skill
  whenever the user asks: "is this branch already pushed?", "can I delete this branch?",
  "which branches are stale?", "clean up merged branches", "remove old worktrees",
  "what branches haven't been pushed yet?", or anything related to branch hygiene,
  branch status, or pruning local/remote branches. Also use it when the user wants to
  delete branches in bulk, either locally or on the remote.
---

# Branch Audit

This skill bundles `scripts/branch-audit.sh` (in this skill's directory) that categorises
every local branch and identifies stale remote branches. Always use it rather than
running one-off `git branch` commands. Run it from the repo root of the repository being
audited. If the target repo has its own `scripts/branch-audit.sh`, prefer that copy.

The default branch is auto-detected from `origin/HEAD` (falling back to `main`);
override with `MAIN=<branch>` or `REMOTE=<remote>` env vars if needed.

## When to use each flag

Below, `branch-audit.sh` means the bundled script — invoke it by its path inside this
skill's directory, e.g. `bash <skill-dir>/scripts/branch-audit.sh`.

| Goal | Command |
|------|---------|
| See branch status (read-only) | `bash branch-audit.sh` |
| Preview what cleanup would do | `bash branch-audit.sh --clean-all --dry-run` |
| Remove stale worktrees + delete merged local branches | `bash branch-audit.sh --clean-local` |
| Delete merged branches on `origin` | `bash branch-audit.sh --clean-remote` |
| Do both at once | `bash branch-audit.sh --clean-all` |

Always run `--dry-run` first when the user hasn't explicitly said "go ahead and delete".

## Output categories

- **Green — merged into main**: safe to delete locally. Branches with an associated
  worktree show the worktree path; clean-local removes both.
- **Purple — remote merged into main**: the remote counterpart is also merged; clean-remote
  deletes them from `origin`.
- **Red — unmerged / dirty**: has commits not in main and is in sync with remote. Needs
  manual review before deletion.
- **Yellow — unpushed**: has local commits not yet pushed to remote, or no remote branch
  at all.
- **Cyan — diverged**: local and remote have both moved on; requires manual reconciliation.

## Workflow

1. Run the audit (no flags) and share the output with the user.
2. If the user wants to clean up, run `--dry-run` first so they can confirm.
3. Once confirmed, run without `--dry-run`.
4. For remote deletion, remind the user this pushes a delete to `origin` — make sure
   they're OK with that before proceeding.

## Answering "is branch X already pushed?"

Look at the Yellow section. If the branch appears there with "no remote branch" or
"N unpushed", it is **not fully pushed**. If it's in Green or not listed at all
(it may already be gone), it is pushed and merged. If it's in Red, it's pushed but
not yet merged into main.
