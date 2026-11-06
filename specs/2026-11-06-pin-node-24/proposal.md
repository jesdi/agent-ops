# Pin Node 24 for the frontend and CI

## Why

The frontend build needs `node:util.styleText` (rolldown via vite), which Node 21.6.1 lacks, so the
build failed on an older local Node. Nothing in the repo says which Node the frontend expects: there
is no `frontend/.node-version` or `.nvmrc` and no `engines` field in `frontend/package.json`. CI runs
Node 22 (`.github/workflows/ci.yml`, jobs `frontend` and `e2e`) while the dev machine runs Node 24
(v24.21.0), so the two can disagree and a version change can drift in unnoticed.

Operator decision (2026-11-05, issue #49): **Node 24 is the one supported Node version for the
frontend, locally and in CI.**

## For whom

The operator and every agent session that builds or tests the frontend, locally or in CI.

## Goal

The repo states Node 24 as the frontend's Node version in one place a version manager reads
(`frontend/.node-version`), enforces a floor in `frontend/package.json` (`engines.node: ">=24"`),
and both frontend CI jobs run on Node 24.

## Non-goals

- The session/setup container image's Node. Containers run the image named by
  `AGENT_OPS_SESSION_IMAGE` (default `agent-ops-session`, `dispatcher/containers.py`); it is built
  from `jesdi/agent-ops-infra`, not this repo, and this repo has no Containerfile. If that image's
  Node is not 24, it needs a separate issue in agent-ops-infra.
- Local machine setup (already done: node v24.21.0) and choosing between nvm and fnm.
- Changing the pnpm version or adding `packageManager`: CI sets pnpm through
  `pnpm/action-setup`'s `version: 10`, and that action errors if `packageManager` is also present
  and disagrees. One source of truth stays in CI.
- Regenerating or editing `frontend/pnpm-lock.yaml`.
- Python (`.python-version`) and the root `pnpm-lock.yaml` (skills tooling).
