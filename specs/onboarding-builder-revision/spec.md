# Rebuild onboarding PRs after agent-ops updates — Spec

## Requirements

1. Record the running agent-ops revision associated with a successfully built open infra PR, separately from the proposal revision and infra base SHA.
2. Rebuild an open PR when its recorded builder differs from the running revision, including while config-merge is waiting or blocked on checks or a human merge. Preserve its PR number and URL and reset the CI waiting window after a successful rebuild.
3. When builder, proposal, infra base and owned head are unchanged, do not push again or reset the CI waiting window. Existing proposal and base change handling remains effective.
4. Load legacy records without builder provenance and refresh their open PR once. Do not invent provenance if the current revision cannot be determined; report a retryable failure without pushing or recording success.
5. Preserve branch ownership, failed-push recovery and manual merge boundaries. Failed or blocked rebuilds must not record new builder provenance or a new successful-push time. Merged and closed PRs are not rebuilt merely because the builder changed.
6. A run uses a consistent builder identity from the executing agent-ops checkout, independent of the process working directory, the infra clone and the desired deployment pin. Do not relabel an adopted pre-existing remote commit as built by this revision without evidence.

## Scenarios

### Scenario: Initial build records provenance

- **Given** confirmed project analytics, running agent-ops revision A and no infra PR.
- **When** onboarding successfully builds and opens the PR.
- **Then** its durable record associates the successful push with A and retains the proposal digest and infra base independently.

### Scenario: Reproduce the deployment race end to end

- **Given** revision A built PR 7 on infra base M1; infra main advances to M2 to deploy B; the next timer run still uses A and rebuilds PR 7 on M2.
- **When** B is installed and onboarding runs again with M2 and the confirmed proposal unchanged.
- **Then** the same PR 7 is rebuilt by B, the record identifies B, and the operator sees that CI is awaited for the rebuilt head with a fresh push timestamp. Exercise the public onboarding run with durable state across runs and controlled external git/GitHub boundaries, reproducing the failure before implementation.

### Scenario: Stable builder converges

- **Given** PR 7 was successfully rebuilt by B at 12:40 with unchanged proposal, base and head.
- **When** B runs again at 12:41 and 12:42.
- **Then** neither run pushes or creates a PR, the push time remains 12:40, and checks/manual merge waiting proceed normally.

### Scenario: Existing invalidation still works

- **Given** B built PR 7 on M2.
- **When** infra main becomes M3, or the operator confirms a changed proposal while B stays installed.
- **Then** onboarding retains its existing rebuild behavior and records B on the successful replacement.

### Scenario: Legacy open record

- **Given** an existing PR 7 record has no builder field and otherwise matches the running proposal and infra base.
- **When** B runs twice successfully.
- **Then** the record loads, the first run refreshes the same PR and records B, and the second run does not rebuild it again.

### Scenario: Builder cannot be resolved

- **Given** an open PR recorded as built by A and an unavailable or empty running revision.
- **When** onboarding attempts to determine freshness.
- **Then** it reports a retryable failure, makes no push and leaves successful provenance and push time unchanged; a later run with known B can rebuild.

### Scenario: Another actor changed the branch

- **Given** the record owns head H1 built by A, but another actor replaced it with unrelated H2.
- **When** B attempts its revision-triggered rebuild.
- **Then** onboarding blocks with the PR link, does not overwrite H2, and retains the prior successful provenance and push time.

### Scenario: Push fails

- **Given** an owned open PR built by A and running revision B.
- **When** the replacement push fails.
- **Then** the record does not claim a successful B build or reset its successful-push time, and a later successful run retries and records B.

### Scenario: Terminal PR states remain respected

- **Given** PR 7 was built by A and B is now running.
- **When** GitHub reports PR 7 merged into main or closed.
- **Then** merged PR processing follows the existing deployment flow and a closed PR retains its existing blocked outcome; neither state triggers a builder-only push or replacement PR.

### Scenario: Revision belongs to the executing checkout

- **Given** the running code belongs to revision B, the working directory is another repository, and infra requests deployment of C.
- **When** onboarding builds an open PR.
- **Then** its builder provenance is B throughout that run, not the working-directory revision, infra SHA or desired C pin.

### Scenario: Recovery does not mislabel older output

- **Given** a push happened before a crash saved its record, and a later run encounters that remote commit with unknown builder provenance.
- **When** onboarding recovers the existing PR using its existing ownership checks.
- **Then** it does not falsely certify that remote commit as produced by the current builder merely because parent and tree match, and converges to a PR actually built by the current revision without overwriting unrelated work.

## Out of scope

Deployment ordering and gating on the desired app-revision; automatic merging; reopening terminal PRs; signing or YAML-format changes; general onboarding lifecycle changes.
