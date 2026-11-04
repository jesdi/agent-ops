# Onboarding increment 8: GitHub App access — Spec

Map https://github.com/jesdi/agent-ops/issues/205, design #212 (A1–A8) and #214. Built as two
stacked PRs: 8a (App module and onboarding, R1–R7 and R12) and 8b (dispatcher host work and
containers, R8–R11).

## Requirements

1. **App identity stays in 1Password.** The App id and private key are read from the 1Password
   item `agent-ops/agent-ops-github-app` (fields `GITHUB_APP_ID`, `GITHUB_APP_CLIENT_ID`,
   `GITHUB_APP_PRIVATE_KEY`) on every mint and never written to disk. Only the
   dispatcher and the onboarding unit mint. No container gets the key or the 1Password token.
2. **App JWT.** Each mint signs an RS256 JWT whose `iss` is the App id, `iat` is 60 seconds in
   the past and `exp` is at most 10 minutes after `iat`.
3. **Narrowed tokens.** A mint names its repositories and permissions. The token request sends
   exactly those, and the result carries the token and its `expires_at`.
4. **No App, no onboarding.** When the 1Password item can't be read, onboarding blocks at resolve
   with the registration checklist from the infra README. Registered projects keep running on
   `GH_REPO_TOKEN`.
5. **Access check at resolve.** Resolve asks GitHub, with the App JWT, for the installation of
   each project repository. A repository the installation doesn't reach blocks resolve with a
   link to that installation's settings, or to the App's install page when the owner has no
   installation. Every timer run checks again, and resolve verifies once every repository is
   reached (map S14). Onboarding never adds a repository to the installation itself.
6. **Recheck after confirm.** The board step checks the installation again. A repository removed
   from it after confirm blocks the board step with the same link.
7. **Onboarding repository work uses the App.** Onboarding's repository calls (the resolve read,
   clone, issue reads and labels) run with `GH_TOKEN` set to one installation token narrowed to
   the project's repositories, with Contents read, Issues write and Metadata read. Board calls
   keep `GH_PROJECT_TOKEN`; infra calls keep `GH_INFRA_TOKEN`.
8. **Dispatcher host work uses the App.** Every dispatcher pass mints one host token per project
   for its GitHub client and workspace git (fetch, PR polling, labels, reconcile). A repository
   the installation doesn't reach falls back to `GH_REPO_TOKEN` (A6).
9. **Task tokens.** At spawn the dispatcher writes `state_dir/tokens/<task>/hosts.yml` holding a
   token narrowed to the task's repositories (all of them for a cross-repository task) with the
   A3 permissions: read and write on Contents, Pull requests, Issues and Workflows; read on
   Actions, Checks and Metadata; nothing else. The directory is mounted read-only at
   `/root/.config/gh` in place of the host's `~/.config/gh`. A task whose repositories the
   installation doesn't all reach is not spawned: it is parked with the installation link, and no
   container ever gets `GH_REPO_TOKEN`.
10. **Refresh and cleanup.** Every pass re-mints a task token with under 20 minutes left and
    replaces `hosts.yml` atomically. The task's token directory is deleted when the task ends.
11. **Triage and inspection.** Triage gets a token for its one repository, mounted the same way.
    Inspection gets no gh mount.
12. **Live smoke check.** `python -m dispatcher.github_app check <owner/repo>` mints a JWT, looks
    up the repository's installation, mints a token narrowed to that repository, and reads it,
    printing each step's result and exiting non-zero at the first failure. Running it on the box
    needs the operator's OK.

## Scenarios

### Scenario: S1 — the JWT carries the App's claims (R1, R2)

- **Given** the 1Password item holds App id `123456` and an RSA private key, and the clock reads
  `2026-11-04T10:00:00Z`
- **When** a token is minted
- **Then** the JWT verifies against the public key with RS256, `iss` is `"123456"`, `iat` is
  `09:59:00`, `exp` is at most `10:09:00`, and no file under
  the state dir or the temp dir holds the key.

### Scenario: S2 — a mint asks for only what it names (R3)

- **Given** installation `9001` reaches `jesdi/analytics` and `jesdi/billing`
- **When** a token is minted for `jesdi/analytics` with Contents read and Issues write
- **Then** the token request lists only `analytics` and only those two permissions, and the
  result holds the token and its `expires_at` of `11:00:00`.

### Scenario: S3 — no App registered blocks onboarding (R4)

- **Given** `op read` of the App item fails and portfolio_eval is already registered
- **When** onboarding analytics runs
- **Then** resolve is blocked with the registration checklist link, no clone runs, and the
  dispatcher's next pass for portfolio_eval still uses `GH_REPO_TOKEN`.

### Scenario: S4 — a repository outside the installation blocks, then resumes (R5)

- **Given** installation `9001` on `jesdi` covers selected repositories, not `jesdi/billing`
- **When** onboarding billing runs
- **Then** resolve is blocked with a link to installation `9001`'s settings and no clone runs;
  **and when** the operator adds billing to the installation and the next timer run comes,
  resolve verifies and clone proceeds.

### Scenario: S5 — an owner with no installation links to the install page (R5)

- **Given** the App has no installation on the organization `acme`
- **When** onboarding `acme/web` runs
- **Then** resolve is blocked with a link to the App's install page.

### Scenario: S6 — a repository removed after confirm blocks the board step (R6)

- **Given** analytics is confirmed with `jesdi/analytics` and `jesdi/analytics-etl`
- **When** `jesdi/analytics-etl` is removed from the installation before the board step
- **Then** the board step is blocked with the installation settings link and no labels or board
  writes happen.

### Scenario: S7 — each onboarding call carries its own credential (R7)

- **Given** analytics is onboarding and installation token `ghs_onb` was minted for its
  repositories
- **When** clone, issue reads, labels, board calls and the infra PR run
- **Then** clone, issue reads and labels carry `GH_TOKEN=ghs_onb`; board calls carry
  `GH_PROJECT_TOKEN`; infra calls carry `GH_INFRA_TOKEN`; none carries `GH_REPO_TOKEN`.

### Scenario: S8 — host work uses the installation token with fallback (R8)

- **Given** analytics is reached by the installation and portfolio_eval is not
- **When** a dispatcher pass fetches, polls PRs and writes labels
- **Then** analytics' calls carry its installation token and portfolio_eval's carry
  `GH_REPO_TOKEN`.

### Scenario: S9 — a task container sees only its own repositories (R9; map S13)

- **Given** analytics and billing are registered and an analytics task spawns
- **When** the container starts
- **Then** its argv mounts `state_dir/tokens/<task>` read-only at `/root/.config/gh` and never
  the host's `~/.config/gh`; `hosts.yml` holds a token minted for `jesdi/analytics` only with the
  A3 permissions; no mount holds the App key, the 1Password token, `GH_PROJECT_TOKEN` or
  `GH_INFRA_TOKEN`.

### Scenario: S9b — a task the installation doesn't reach is parked (R9)

- **Given** a ready billing task and `jesdi/billing` removed from installation `9001`
- **When** a dispatcher pass would spawn it
- **Then** no container starts, the task is parked with installation `9001`'s settings link, and
  no `hosts.yml` holding `GH_REPO_TOKEN` is written; once billing is reached again, a later pass
  spawns it.

### Scenario: S10 — a cross-repository task gets all its repositories (R9)

- **Given** a task spans `jesdi/analytics` and `jesdi/analytics-etl`
- **When** it spawns
- **Then** its token is minted for exactly those two repositories.

### Scenario: S11 — refresh at the 20-minute boundary (R10; map S12)

- **Given** two running tasks whose tokens expire in 21 and 19 minutes
- **When** a dispatcher pass runs
- **Then** only the second is re-minted, and its `hosts.yml` is replaced by a rename, so a reader
  never sees a partial file.

### Scenario: S12 — the token directory goes with the task (R10)

- **Given** a task reaches a terminal state
- **When** the next pass runs
- **Then** `state_dir/tokens/<task>` no longer exists and no re-mint is attempted for it.

### Scenario: S13 — triage and inspection mounts (R11)

- **Given** a triage run for `jesdi/analytics` and an inspection of `jesdi/billing`
- **When** their containers start
- **Then** triage mounts a token directory for `jesdi/analytics` only, and inspection has no
  `/root/.config/gh` mount.

### Scenario: S14 — the smoke check reports each step (R12)

- **Given** the App is registered and its installation reaches `jesdi/analytics` but not
  `jesdi/billing`
- **When** `check jesdi/analytics` and then `check jesdi/billing` run
- **Then** the first prints JWT, installation, token and repository read as passed and exits 0;
  the second stops at the installation step with its link and exits non-zero.

## Out of scope

Adding repositories to the installation by API, App access to boards, any change to infra
credentials, retiring `GH_REPO_TOKEN` (R21 migration), automatic App registration, project
secrets, verification, console UI changes, and anything run on the box.
