# Pin Node 24 for the frontend and CI — Spec

Source: issue #49 (rewritten 2026-11-05 in triage; operator decision of the same date). The issue
carries a settled design; this spec carries it over word for word where it decides something and
records how the current code matches it. No questionnaire was raised: no decision was open.

## Settled decisions (from the issue, verbatim)

> Node 24 is the one supported Node version for the frontend, locally and in CI (operator
> decision, 2026-11-05).

> 1. Create `frontend/.node-version` containing exactly `24` and a trailing newline.
> 2. In `frontend/package.json`, add a top-level `"engines": { "node": ">=24" }`. Do not add
>    `packageManager`: CI sets the pnpm version through `pnpm/action-setup`'s `version: 10`, and
>    that action errors if `packageManager` is also present and disagrees. One source of truth
>    stays in CI.
> 3. In `.github/workflows/ci.yml`, change `node-version: 22` to `node-version: 24` in both jobs
>    (lines 33 and 57). Leave everything else unchanged.
> 4. Do not edit `frontend/pnpm-lock.yaml`. If `pnpm install --frozen-lockfile` fails under
>    Node 24, report it in the PR rather than regenerating the lockfile.

## Reconciliation with the code (main at 69f00f9)

The issue's line numbers were taken at `61dbbdb`. Checked against the current tree:

- `frontend/.node-version` and `frontend/.nvmrc` still do not exist; neither does a root
  `.node-version`/`.nvmrc`.
- `frontend/package.json` still has no `engines` and no `packageManager` field.
- `.github/workflows/ci.yml` still has exactly two `node-version:` lines, both `22`, at lines 33
  (job `frontend`) and 57 (job `e2e`), each under `actions/setup-node@v4`, each preceded by
  `pnpm/action-setup@v4` with `version: 10`. Nothing moved.
- `.github/workflows/deploy-pin.yml` does not mention Node; it is untouched.
- `frontend/pnpm-lock.yaml` is `lockfileVersion: '9.0'`, as the issue says.
- `frontend/package.json` already depends on `@types/node ^24.13.2`, consistent with Node 24.
- The box this spec was written on runs node v24.18.0, which satisfies `>=24`; the `24` in
  `.node-version` resolves to the latest 24.x on a version manager and in `actions/setup-node`.

No settled decision is contradicted by the code.

## Requirements

1. `frontend/.node-version` exists and its content is exactly `24\n` (the two characters `24`
   followed by one newline, nothing else).
2. `frontend/package.json` is valid JSON with a top-level `engines` object whose `node` value is
   exactly `">=24"`, and it has no `packageManager` field. Every other key and value in the file
   is unchanged.
3. In `.github/workflows/ci.yml`, both `node-version:` lines read `24`: `grep -n "node-version"
   .github/workflows/ci.yml` prints exactly two matches, both `node-version: 24`, none `22`. No
   other line of the workflow changes (pnpm `version: 10`, `cache`, `cache-dependency-path` and
   the steps stay as they are).
4. `frontend/pnpm-lock.yaml` is not modified by the change.
5. The `frontend` and `e2e` CI jobs pass on the PR and their `actions/setup-node` step log shows
   a Node 24.x version.
6. From `frontend/` on Node 24, `pnpm install --frozen-lockfile`, `pnpm build` and `pnpm test`
   succeed. If `pnpm install --frozen-lockfile` fails under Node 24, the PR reports the failure
   instead of regenerating the lockfile (requirement 4 holds either way).
7. The change touches exactly three files: `frontend/.node-version` (new),
   `frontend/package.json` and `.github/workflows/ci.yml` (plus this spec folder).

## Scenarios

### Scenario: version file pins 24 (R1)

- **Given** the task branch after the change
- **When** reading `frontend/.node-version` as bytes
- **Then** the content is exactly `32 34 0a` (`24` and a newline)

### Scenario: version file with a full version is a violation (R1)

- **Given** `frontend/.node-version` containing `24.21.0\n` or `v24\n` or `24` with no newline
- **When** the R1 check runs
- **Then** it fails: the decision is the major version `24` alone, with a trailing newline

### Scenario: engines floor present, no packageManager (R2)

- **Given** the task branch after the change
- **When** running `node -e 'const p=require("./frontend/package.json"); console.log(p.engines.node, "packageManager" in p)'`
- **Then** it prints `>=24 false`

### Scenario: packageManager added is a violation (R2)

- **Given** `frontend/package.json` that also carries `"packageManager": "pnpm@10..."`
- **When** the R2 check runs
- **Then** it fails, because `pnpm/action-setup` with `version: 10` would conflict with it

### Scenario: package.json otherwise unchanged (R2, R7)

- **Given** `git diff origin/main -- frontend/package.json`
- **When** reading the diff
- **Then** the only added lines are the `engines` object (and at most one comma on a neighbouring
  line to keep the JSON valid); no line is removed except to add that comma

### Scenario: CI pins Node 24 in both jobs (R3)

- **Given** the task branch after the change
- **When** running `grep -n "node-version" .github/workflows/ci.yml`
- **Then** it prints two lines, `33:          node-version: 24` and `57:          node-version: 24`

### Scenario: one job left on 22 is a violation (R3)

- **Given** `ci.yml` where only the `frontend` job reads `node-version: 24` and `e2e` still reads `22`
- **When** the R3 check runs
- **Then** it fails: `grep -c "node-version: 22" .github/workflows/ci.yml` must print `0`

### Scenario: workflow otherwise unchanged (R3)

- **Given** `git diff origin/main -- .github/workflows/ci.yml`
- **When** reading the diff
- **Then** it changes exactly two lines, each `node-version: 22` → `node-version: 24`

### Scenario: lockfile untouched (R4)

- **Given** the task branch after the change
- **When** running `git diff --quiet origin/main -- frontend/pnpm-lock.yaml`
- **Then** it exits 0

### Scenario: lockfile edited is a violation (R4)

- **Given** a branch where `pnpm install` regenerated `frontend/pnpm-lock.yaml`
- **When** running `git diff --quiet origin/main -- frontend/pnpm-lock.yaml`
- **Then** it exits 1 and the change is rejected

### Scenario: CI green on Node 24 (R5)

- **Given** the PR for this change
- **When** the `ci` workflow runs on it
- **Then** jobs `frontend` and `e2e` succeed, and each job's `actions/setup-node` step log shows
  `node` resolved to a `v24.` version

### Scenario: a job still on Node 22 is a violation (R5)

- **Given** a `frontend` or `e2e` job whose `actions/setup-node` log shows `v22.`
- **When** the R5 check reads the log
- **Then** it fails, even if the job is green

### Scenario: local install, build and test on Node 24 (R6)

- **Given** `node -v` prints `v24.x` (for example v24.18.0 on the box) and the working directory is `frontend/`
- **When** running `pnpm install --frozen-lockfile && pnpm build && pnpm test`
- **Then** all three exit 0

### Scenario: frozen install fails under Node 24 (R6, R4)

- **Given** `pnpm install --frozen-lockfile` exits non-zero under Node 24
- **When** the implementer meets that failure
- **Then** the lockfile stays unchanged and the PR body reports the command and its error output

### Scenario: nothing else changes (R7)

- **Given** `git diff --name-only origin/main...HEAD`
- **When** listing the changed files
- **Then** the list is `.github/workflows/ci.yml`, `frontend/.node-version`, `frontend/package.json`
  and the two files of `specs/2026-11-06-pin-node-24/`, nothing else

### Scenario: a fourth file changed is a violation (R7)

- **Given** a diff that also touches `frontend/pnpm-lock.yaml` or `.github/workflows/deploy-pin.yml`
- **When** listing the changed files
- **Then** the R7 check fails

## Out of scope

- The session/setup container image's Node (`AGENT_OPS_SESSION_IMAGE`, built in
  `jesdi/agent-ops-infra`); a mismatch there is a separate issue in that repo.
- Local machine setup and the choice of version manager.
- The pnpm version and the `packageManager` field.
- `frontend/pnpm-lock.yaml`, `.python-version` and the root `pnpm-lock.yaml`.
