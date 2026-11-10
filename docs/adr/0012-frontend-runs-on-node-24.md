# 0012. Frontend runs on Node 24

Date: 2026-11-10

## Context

The frontend build requires Node features unavailable on an older local Node. CI used Node 22 while local development used Node 24, and the repository did not declare a supported frontend Node version.

## Decision

Node 24 is the supported frontend Node version locally and in the frontend and e2e CI jobs. `frontend/.node-version` contains the major version `24` for version managers; it is used instead of `.nvmrc` as decided in issue #49. Plain `nvm use` may need a flag. `frontend/package.json` declares `engines.node: ">=24"` as a floor, without a ceiling or `engine-strict`; pnpm may only warn when the engine does not match. CI owns the pnpm version through `pnpm/action-setup` with `version: 10`, so the package manifest has no `packageManager` field.

## Consequences

Frontend builds and tests should use Node 24 locally and in CI. A later Node change must update the version file, package engine floor, and both CI jobs together. Node 25 and later are not blocked by the manifest floor. The session container image and its Node version are maintained in agent-ops-infra.
