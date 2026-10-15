# Project onboarding and isolated multi-project execution

## Why

An operator should be able to add a project to the box without manually joining
GitHub permissions, a backlog board, checkouts, target configuration, secrets, and
test infrastructure. The 2026-10-14 investigation found that the deployed dispatcher
supported multiple targets but configured only `portfolio_eval`, had no separate
agent-ops target checkout, and held 13 unfinished tasks against a shared limit of
10. Adding a configuration entry alone would not have made agent-ops process
work. Onboarding must produce a verified, visible project and capacity allocation
must preserve room for each enabled project. A frontend and backend can live in
different repositories while sharing one project board and tasks that require
changes in both repositories.

## For whom

The box operator, using a terminal on their computer to add projects and either
the terminal or web console to enable and pause them. Repositories may belong to
personal accounts or organizations; an organization administrator may need to
approve GitHub App installation or permissions.

## Goal

Provide a resumable, confirmed onboarding command that provisions a project's
repositories, shared project board, restricted credentials, Compose test services,
task sandboxes, and web tab through versioned infrastructure deployment. Support
tasks with multiple repository PRs while retaining one exclusive unfinished-task
place per enabled project within a total of 10.

## Non-goals

- A repository belonging to multiple agent-ops projects, or a GitHub Projects
  board shared by separate agent-ops projects. Multiple repositories within one
  project and its board are supported.
- Supporting-service definition formats other than Compose in the first version;
  inventing missing service definitions or silently running incompatible ones.
- Running repository setup, dependency installation, tests, or supporting
  services directly on the box host, or outside the box.
- Sharing repository credentials across task sessions or giving task sessions
  the GitHub App private key, board credential, or infrastructure control credential.
- Automatic processing immediately after onboarding; new projects start paused.
- Automatic merging of task PRs, risk classification for that automation, or
  automatic rollback of partially merged changes. Task PRs remain manually
  merged in this version, with explicit dependencies and requests for guidance
  when a required PR is closed without merging. The agreed automatic merge of
  confirmed infrastructure configuration PRs remains in scope.
- Pausing or canceling existing tasks when their project is paused. Project pause
  only stops new claims; existing work and triage can continue.
- Borrowing another enabled project's reserved place, enabling more than 10
  projects, increasing the total unfinished-task limit, or increasing execution
  capacity implicitly during onboarding.
- Bypassing GitHub approvals, branch rules, required checks, or the operator's
  confirmation; editing live targets.yaml as the source of truth.
- Destructive rollback of partial onboarding, removal of unrelated board cards,
  automatic cancellation of existing work to satisfy new limits, or project
  removal/renaming workflows in this slice.
- Making every project author a new token or complete all setup manually, and
  treating read-only access checks as proof that GitHub writes will succeed.

The accepted requirements are in [spec.md](spec.md). Existing application
semantics live in [CONTEXT.md](../../CONTEXT.md) and
[README.md](../../README.md); earlier multi-target requirements are recorded in
[the multi-project spec](../multi-project-box/spec.md). Infrastructure deployment
ownership is documented in
[agent-ops-infra](https://github.com/jesdi/agent-ops-infra), particularly its
README, `config/targets.yaml`, `provision/update.sh`, and
`docs/runbooks/github-credentials.md`. The observations above are historical,
not a claim about the box's state when implementation starts.

Application implementation must pass the documented `make gate`. Infrastructure
implementation must pass that repository's documented test command; its current
Makefile exposes `make test`. This change spans both repositories, while these
files define one user-facing feature. [design.md](design.md) records stage 2;
the local implementation graph lives under `.agent/tickets/` and is not committed.
