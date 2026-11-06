# Pin Node 24 for the frontend and CI — Design

## Decisions

- One ticket: three one-line config edits share one verification (install, build, test on Node 24); splitting would give three tickets that cannot be demoed alone.
- `.node-version` over `.nvmrc`: both fnm and nvm-compatible tools read it; the issue chose it. Tradeoff: plain `nvm use` needs a flag.
- `engines.node: ">=24"` is a floor, not a ceiling: Node 25+ is not blocked. pnpm only warns on a mismatch unless `engine-strict` is set; we do not set it.
- No `packageManager`: `pnpm/action-setup` `version: 10` is the single pnpm source.
- Lockfile untouched; a frozen-install failure is reported in the PR, not fixed.

## Data model

None.

## Identities

None.

## Seams

Shell checks against the repo, all existing: `git diff origin/main`, `grep node-version .github/workflows/ci.yml`, `node -e` on `frontend/package.json`, and `pnpm install --frozen-lockfile && pnpm build && pnpm test` from `frontend/`. The CI run of the PR covers R5.
