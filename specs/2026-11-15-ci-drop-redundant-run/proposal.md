# CI: drop the redundant --run from the frontend test step

## Why

The `frontend` job in `.github/workflows/ci.yml` runs `pnpm test -- --run`. The `test` script in
`frontend/package.json` is already `vitest run`, which never enters watch mode, so the extra
`--run` does nothing. It is noise that suggests the script might start watch mode, and it makes
CI differ from the box's `make gate`, which already runs plain `pnpm test`.

## For whom

Maintainers and pipeline sessions reading or editing the CI workflow.

## Goal

The frontend CI job runs plain `pnpm test`, identical in behaviour to today, with the diff
touching only `.github/workflows/ci.yml`.

## Non-goals

- The double `try-restart` when one commit touches both `frontend/` and `web/`: it lives in
  jesdi/agent-ops-infra (`provision/update.sh`) and is tracked by jesdi/agent-ops-infra#28.
- The "misleading queue-boost test title": the test it most likely named
  (`frontend/src/pages/__tests__/QueuePage.test.tsx`) was deleted with the Queue page
  (commit 4b0da0d); no surviving test can be tied to the report, so the item is dropped.
- Changing `frontend/package.json` (its `"test": "vitest run"` stays as is).
