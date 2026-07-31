# Spec visibility before human review

**Date:** 2026-07-31
**Status:** Draft — awaiting human review
**Scope:** box pipeline only (dispatcher-spawned SPEC-stage sessions). Interactive/local brainstorming sessions are unchanged.

## Problem

The SPEC stage writes its draft to the worktree and explicitly does **not**
commit until the human approves in an attached chat session
(`prompts/spec.md`). Until then the spec exists only inside one box's
worktree: it cannot be read from another machine, another session, or a
phone. The Telegram "spec ready" notification arrives, but the artifact it
announces is unreachable — the reviewer must attach a terminal to read it.

## Design

### 1. Prompt change — `prompts/spec.md`

The SPEC stage flow becomes commit-early, push-early:

- **Draft ready:** commit the spec to `$branch` with message
  `docs: draft spec for #$issue_number`, push to `origin`, **then** write
  `.agent/stage.json` with `status: awaiting-review`.
- **Review iteration:** after each meaningful revision requested in chat,
  commit and push again so the GitHub view stays current.
- **Approval:** final commit with the existing message
  (`docs: spec for #$issue_number (agent-ops)`) and push, then exit.

The "do NOT commit yet" instruction and the resume-from-uncommitted-draft
wording are removed; resume now means "read the spec already committed on
`$branch` (or any uncommitted edits) and continue".

### 2. Dispatcher verify — deterministic backstop

A prompt is an instruction, not a guarantee (same reasoning as the triage
decide/apply split). When `machine.py` handles `status: awaiting-review`
for a task in stage `SPEC`, before transitioning to
`AWAITING_SPEC_REVIEW` and notifying, the dispatcher verifies the spec is
actually visible on GitHub:

- **Committed?** If the artifact file has uncommitted changes (or is
  untracked) in the worktree, the dispatcher commits it itself to
  `$branch` (`docs: draft spec for #$issue_number`), staging only the
  artifact path.
- **Pushed?** If the branch tip containing the artifact is not on
  `origin` (compare local tip vs `git ls-remote origin <branch>`), the
  dispatcher pushes.
- **Push fails** (credentials, network): the task still parks for review —
  review in the attached session must not be blocked by GitHub being
  unreachable — but the Telegram notification says the spec is
  **local only** and includes the error one-liner instead of a link.

Verification runs in the task's worktree via the same subprocess-git
plumbing the dispatcher already uses (`workspace.py`); no new git
abstraction.

### 3. Surfacing the link — dispatcher-side

The dispatcher already has `$repo`, `$branch`, and the artifact path
(`signal.artifact` from `stage.json`), so it builds the URL
deterministically:

```
https://github.com/<repo>/blob/<branch>/<artifact-path>
```

On the (verified) transition to `AWAITING_SPEC_REVIEW` it:

- **Comments on the task's issue** via the existing `GitHubClient`:
  `Spec ready for review: <url>`. The issue is where the task lives and
  GitHub notifications reach the phone.
- **Includes the same URL in the `awaiting_spec_review` Telegram
  notification**, so the message on the phone is one tap from the
  rendered spec.

The model is never asked to construct or report the URL — it can't get it
wrong or skip it.

### Error handling summary

| Failure | Behavior |
| --- | --- |
| Agent forgot to commit | Dispatcher commits the artifact file itself |
| Agent forgot to push | Dispatcher pushes |
| Push rejected / no credentials / offline | Park for review anyway; Telegram says "spec is local only: <error>"; no issue comment |
| Issue comment fails | Telegram link already sent; log and continue (comment is best-effort) |

### Testing

- Unit tests on the `machine.py` transition: awaiting-review at SPEC stage
  emits verify+comment+notify actions; non-SPEC stages unchanged.
- Tests for the verify helper: uncommitted artifact → commit action;
  unpushed branch → push; both clean → no-op; push failure → local-only
  notification text.
- Notification/comment formatting tests: URL built from repo + branch +
  artifact path, including artifact paths with subdirectories.
- Prompt template test: `spec.md` still renders with the existing context
  variables (strict `Template.substitute`).

## Out of scope

- Draft PRs at spec stage.
- Moving approval itself to GitHub — review and approval stay in the
  attached chat session.
- Any change to interactive (non-box) brainstorming sessions.
- Pushing PLAN/IMPLEMENT-stage artifacts early (implement already ends in
  a pushed PR).
