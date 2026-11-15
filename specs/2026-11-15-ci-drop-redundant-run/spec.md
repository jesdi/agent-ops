# CI: drop the redundant --run from the frontend test step — Spec

Source: issue #47 (rewritten 2026-11-05 in triage). The issue settles the whole change; no
questionnaire was raised because no decision was open.

## Settled change (from the issue, verbatim)

> Change the step at `.github/workflows/ci.yml:42` to `- run: pnpm test`. Touch nothing else.

## Reconciliation with the code (main at 4b48ccc)

The issue's line numbers were taken at `61dbbdb`. Checked against the current tree:

- `.github/workflows/ci.yml:42` is still `      - run: pnpm test -- --run`, the last step of
  the `frontend` job, right after `- run: pnpm build` (line 41). It is the only `--run` in any
  workflow (`deploy-pin.yml` has none).
- `frontend/package.json` now has the `test` script at line 15 (not 10): `"test": "vitest run"`.
  A `"test:watch": "vitest"` script also exists; CI does not use it.
- The `e2e` job runs `pnpm test:e2e`, not `pnpm test`; it is untouched.
- The `Makefile` `gate` target already runs `cd frontend && ... && pnpm test` with no `--run`, so
  after the change CI and the box gate invoke the frontend suite the same way.

Nothing moved underneath the settled change.

## Requirements

1. No `pnpm test` invocation in `.github/workflows/ci.yml` carries a `--run` flag:
   `grep -n -- '--run' .github/workflows/ci.yml` prints nothing and exits 1.
2. The `frontend` job's last step is exactly `- run: pnpm test`, and it still comes directly
   after `- run: pnpm build`; every other step of every job is unchanged.
3. `cd frontend && pnpm test` exits 0 and terminates on its own (vitest single run, no watch
   mode), as before.
4. The change's diff against `origin/main` touches exactly one file outside `specs/`:
   `.github/workflows/ci.yml`, with one line changed.
5. The workflow stays valid YAML and the `frontend` CI job passes on the PR.

## Scenarios

### Scenario: the redundant flag is gone (R1)

- **Given** the task branch after the change
- **When** `grep -n -- '--run' .github/workflows/ci.yml` runs
- **Then** it prints nothing and exits with status 1

### Scenario: the flag reappears (R1, violation)

- **Given** a workflow whose frontend step reads `- run: pnpm test -- --run`
- **When** the same grep runs
- **Then** it prints `42:      - run: pnpm test -- --run` and exits 0, so the requirement fails

### Scenario: step order is kept (R2)

- **Given** the task branch after the change
- **When** the `frontend` job's steps are read from `.github/workflows/ci.yml`
- **Then** the last two `run:` steps are `pnpm build` then `pnpm test`, and
  `git diff origin/main -- .github/workflows/ci.yml` shows exactly one removed line
  (`- run: pnpm test -- --run`) and one added line (`- run: pnpm test`)

### Scenario: local run does not enter watch mode (R3)

- **Given** `frontend/` with dependencies installed (`pnpm install --frozen-lockfile`)
- **When** `cd frontend && pnpm test` runs in a non-interactive shell with a 10-minute timeout
- **Then** vitest runs the suite once, exits 0, and the command returns before the timeout

### Scenario: the diff stays in one file (R4)

- **Given** the task branch after the implement stage
- **When** `git diff --name-only origin/main...HEAD -- . ':!specs'` runs
- **Then** it prints exactly `.github/workflows/ci.yml`

### Scenario: CI still green (R5)

- **Given** the PR for this change is open
- **When** the `ci` workflow runs on it
- **Then** the `frontend` job succeeds, including its `pnpm test` step, and `make gate` passes
  locally

## Out of scope

- `frontend/package.json` stays unchanged (its `test` script is already non-watch).
- The double `try-restart` (jesdi/agent-ops-infra#28) and the dropped queue-boost test-title
  item, as described in `proposal.md`.
