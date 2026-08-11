# Multi-target onboarding: consume agent-ops issues (design)

Date: 2026-08-11
Status: approved in discussion, pending spec review

## Goal

The box currently consumes issues from one target (`portfolio_eval`).
Onboard `jesdi/agent-ops` — this repo — as a second consumption target,
document the onboarding as a repeatable runbook for future targets, and
dogfood the first agent-ops task through the pipeline: a board view that
lets the operator filter the console by target.

## What already exists (verified, no work needed)

- **Dispatcher and web API are multi-target.** `targets:` in
  `targets.yaml` is a list; `run_pass` iterates all targets and degrades
  per-target on failure; cross-repo issue-number ambiguity is rejected
  with 409s; board cards and Queued ghosts carry a `target` field.
  No dispatcher, API, or read-model changes are required.
- **The backlog skill is published and pinned.** `backlog@0.1.6` lives
  in `jesdi/general-skills`; agent-ops's committed `.my-skills.json`
  already pins it. `npx -y @jesdi/skills-cli sync` materialises it into
  `.claude/skills/backlog/` (gitignored), which is where `rank_cmd`
  finds `rank.py`.
- **The project board exists with the full schema.** User project #3
  ("agent-ops", https://github.com/users/jesdi/projects/3) has Status
  (Inbox / Ready / In progress / Done), Impact, Effort, Score, Boost,
  and Area — the same field set the backlog skill's `setup` verb would
  create. Field/option IDs are recorded in the project-meta file below.
- **Credentials cover agent-ops.** The fine-grained PAT has push on
  `jesdi/agent-ops` (verified via `gh api repos/jesdi/agent-ops
  -q .permissions`), and the classic project-scope token
  (`GH_PROJECT_TOKEN`, fed per-pass by `op run`) reads project #3.

## Work package 1 — assets committed to agent-ops (one PR)

1. **`.backlog/project-meta.json`** — board #3's IDs, same shape as
   portfolio_eval's committed copy. `rank.py` locates it by walking up
   from cwd, so it must sit at the repo root's `.backlog/` directory.
   Values (from `gh project field-list 3 --owner jesdi`):
   - project: owner `jesdi`, number `3`
   - Status `PVTSSF_lAHOAD7w4M4BebkazhY12cA`
     (Inbox `1709ac25`, Ready `89e7db7c`, In progress `ca75ded8`,
     Done `d7243ba5`)
   - Impact `PVTF_lAHOAD7w4M4BebkazhY12gQ`,
     Effort `PVTF_lAHOAD7w4M4BebkazhY12gU`,
     Score `PVTF_lAHOAD7w4M4BebkazhY12gY`,
     Boost `PVTF_lAHOAD7w4M4BebkazhY12hQ`
   - Area `PVTSSF_lAHOAD7w4M4BebkazhY12iI`
     (feature `f3255170`, bug `b53746f3`, infra `cfe58b60`,
     docs `3674dec3`, research `cffeb17b`)
   - The project node id (`projectId`) is fetched during implementation
     with the project-scope token (`gh project view 3 --owner jesdi
     --format json -q .id`).
2. **`scripts/provision-worktree.sh`** — idempotent worktree setup,
   modeled on portfolio_eval's. Runs inside the one-shot session-image
   container, cwd = the fresh worktree. Steps: Python deps
   (`pip install -e ".[dev]"`), frontend deps (`pnpm install` in
   `frontend/`), repo skills (`npx -y @jesdi/skills-cli sync`).
3. **`Makefile` with a `verify` target** — `pytest` plus the frontend
   vitest run, so the target's `verify_cmd` is `make verify`, mirroring
   portfolio_eval's `make e2e-remote` shape. The exact frontend test
   invocation follows `frontend/package.json`'s existing script.
4. **`docs/runbooks/onboard-target.md`** — the repeatable checklist for
   adding any future target:
   1. Create (or reuse) a Projects v2 board with the backlog skill's
      field schema; record IDs in a committed `.backlog/project-meta.json`.
   2. Verify PAT coverage (`gh api repos/<owner>/<repo> -q .permissions`)
      and add the repo to the fine-grained PAT's list if missing.
   3. Add repo assets: rank source (`.my-skills.json` pin + sync),
      `scripts/provision-worktree.sh`, a verify command.
   4. Clone to `/home/agent/repos/<name>` over https; run the skills
      sync once in the clone.
   5. Append the target block to `$STATE_DIR/targets.yaml`.
   6. Verify with a dispatcher pass: the target's Ready issues appear as
      Queued ghosts and in the next-claim line on the console.
   The runbook calls out the sharp edge explicitly: **the work clone
   must never be the deployed checkout** (`/home/agent/agent-ops`),
   which `agent-ops-update.timer` owns (`git pull --ff-only` on main).

## Work package 2 — box-side ops (state dir, not committed)

- Clone `jesdi/agent-ops` to `/home/agent/repos/agent-ops` (https
  remote, matching the credential model) and run
  `npx -y @jesdi/skills-cli sync` in the clone. Worktrees live at
  `/home/agent/repos/agent-ops/.worktrees`, matching the live
  portfolio_eval layout.
- Append to `/home/agent/agent-ops-state/targets.yaml`:

  ```yaml
  - name: agent_ops
    repo: jesdi/agent-ops
    clone_path: /home/agent/repos/agent-ops
    worktrees_path: /home/agent/repos/agent-ops/.worktrees
    rank_cmd: "python3 .claude/skills/backlog/rank.py --json"
    setup_cmd: "scripts/provision-worktree.sh"
    verify_cmd: "make verify"
    project_number: 3
    project_owner: jesdi
    status_field_id: PVTSSF_lAHOAD7w4M4BebkazhY12cA
    status_ready_option_id: 89e7db7c
    status_in_progress_option_id: ca75ded8
    status_done_option_id: d7243ba5
    boost_field_id: PVTF_lAHOAD7w4M4BebkazhY12hQ
  ```

  (`targets.example.yaml` gains the same block with placeholder IDs so
  the example stays representative of a multi-target setup.)

## Work package 3 — dogfood issue: board target filter tabs

Filed via the backlog skill (capture → triage → Ready) as the first
agent-ops issue the box consumes. Scope for that issue:

- A tab strip on `BoardPage` — `[All] [portfolio_eval] [agent_ops]` —
  where target names are derived from the board/queue data, never
  hardcoded.
- Selecting a tab filters column cards and Queued ghosts to that
  target; capacity, budget, and the next-claim line stay global.
- Each target tab shows a card count so activity on a non-selected
  target is never invisible.
- Selection persists in the existing `useUiStore`.
- Vitest coverage for the filtering behaviour.

The issue's spec/plan/implement stages run through the normal pipeline;
its PR is the end-to-end proof of the onboarding.

## Error handling and risks

- **Self-hosting:** merged `main` is the only deploy path (ADR 0001's
  pull-based convergence, CI-gated, operator PR review). A task branch
  in the work clone cannot affect the running box.
- **Per-target degradation:** an agent-ops rank/setup failure marks that
  target's pass failed and continues with the others (existing
  dispatcher behaviour), so a bad onboarding cannot stall
  portfolio_eval.
- **Issue-number collisions:** already handled — queue writes 409 on
  cross-target ambiguity; ghost keys and next-claim matching include
  the target.
- **Deployed-checkout hazard:** documented in the runbook; the target
  block's `clone_path` is `/home/agent/repos/agent-ops`, never
  `/home/agent/agent-ops`.

## Testing / acceptance

1. `make verify` passes locally in the work clone.
2. `rank_cmd` run in the clone returns ranked JSON for agent-ops's open
   Ready issues.
3. A dispatcher pass after the targets.yaml change shows agent-ops
   ghosts in the console's Queued column and a sane next-claim verdict.
4. The dogfood issue flows spec → operator review → plan → implement →
   PR, and the merged tabs feature filters the board by target.

## Out of scope

- Dispatcher/API changes (none needed).
- Making `rank_cmd`/`setup_cmd` optional with built-in fallbacks
  (revisit if a third target makes the runbook feel heavy; capture as a
  backlog idea at most).
- Migrating portfolio_eval's skill layout (already CLI-managed).
