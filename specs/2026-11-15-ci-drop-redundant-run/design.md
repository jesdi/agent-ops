# CI: drop the redundant --run from the frontend test step — Design

## Decisions

- **Edit the one workflow line, nothing else** — `frontend/package.json` already has `"test": "vitest run"`, so no script change is needed; the tradeoff is that CI now relies on that script staying non-watch (a future `"test": "vitest"` would hang CI, and `make gate` already has that dependency).
- **Verify by grep and diff, not a new test** — the change is one deleted flag; a unit test of a YAML line would cost more than the flag, so the scenarios are checked with `grep`, `git diff` and the existing gate.

## Data model

None. No tables, fields or constraints are touched.

## Identities

None.

## Seams

- `.github/workflows/ci.yml`, the `frontend` job's last step: existing; checked with `grep -n -- '--run'` and `git diff origin/main`.
- `cd frontend && pnpm test`: existing; the single-run command the step invokes.
- `make gate`: existing; the repository gate.
