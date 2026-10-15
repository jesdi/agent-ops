# Project onboarding and isolated multi-project execution — Design

## Decisions

- **Approved scope** — implement R1–R26 and S01–S66 in [spec.md](spec.md); task-PR risk scoring, automatic task-PR merges, and automatic rollback remain future work, while confirmed infrastructure PRs can auto-merge.
- **Project is the board and scheduling boundary** — one configured project owns one board and a nonempty repository set; repository identity is unique across projects, so adding repositories does not add reservations, tabs, or project model policies.
- **Explicit identities** — use a stable project key, GitHub repository ID, and repository-local issue number for task identity; retain canonical repository names and GitHub node IDs for lookup/display, at the cost of migrating all old target/issue consumers.
- **One originating issue per claimed task** — preserve the current issue-to-task claim contract; the task can affect several project repositories and require several PRs, without automatically coalescing separate frontend/backend issues into one task.
- **Expand, migrate, contract** — introduce the widened identity beside the single-repository representation, migrate storage and callers in bounded slices, then retire ambiguous legacy writes; legacy records resolve only through their known single-repository mapping, never by guessing from issue number or title.
- **Existing storage architecture** — retain versioned YAML for desired configuration and validated, atomically replaced box state for runtime records; no new SQL database is required by this feature, but uniqueness, references, compare-and-swap revisions, and admission constraints below must be enforced by the persistence boundary under its owning lock.
- **Local command and trusted control service** — the CLI collects and confirms a plan locally; durable execution and status are owned on the box so SSH interruption is recoverable, and the web submits the same confirmed operations without receiving control credentials.
- **Desired state is distinct from applied state** — enable/pause is a versioned configuration change; operation progress and reservation holds are runtime state, not an overriding enabled flag. Effective state changes only on verified application of the exact configuration revision.
- **Confirmation binds a plan revision** — configuration delta, repository membership, commands, service definitions, secret references, and external write tests belong to the confirmed operation; a materially different proposal cannot reuse an earlier confirmation.
- **Recover external writes by identity** — each operation records intended steps and observed GitHub/resource IDs; a retry reconciles an uncertain outcome before repeating a create, which avoids duplicates without claiming a distributed transaction with GitHub.
- **Checked PR publication** — configuration PRs are rebuilt against current infrastructure main and validated before merge and deployment; branch protections and outside approvals remain authoritative, and unrelated concurrent changes are preserved.
- **Short admission critical sections** — claims, reservations, slot ownership, and configuration application share the existing box convergence boundary; no GitHub wait, human approval wait, sandbox execution, or network call holds that lock indefinitely.
- **Exclusive unfinished-task places** — reserve one place per enabled project, protect empty reservations, and count paused work toward the same total of 10; this intentionally leaves capacity idle when only another project's reserved place remains.
- **Pending enable handoff** — when an enable can fit, its operation holds the new reservation while publication/deployment proceeds; claims account for that hold, effective state stays paused until deployment, and a failed or withdrawn operation releases only its own hold. No new timeout or automatic task eviction is introduced.
- **Execution slots have typed owners** — normal task execution, triage, and onboarding verification share one slot pool; verification owns no unfinished-task place, while parked task data has no live service resource allocation after services stop.
- **Dedicated board, repository-aware contents** — a board may hold issues from all project members, but not outside membership; joins use repository-qualified issue/node identity, and redacted or ambiguous content is reported rather than guessed by title.
- **Future issue population uses reconciliation** — initial import and later deterministic issue-to-board reconciliation share one idempotent operation; this meets automatic population without assuming GitHub's native auto-add workflow settings have a writable API. Reuse the existing dispatcher cadence rather than inventing a new polling deadline.
- **Central authority, restricted session grants** — the trusted credential service issues repository-scoped App tokens bound to recorded execution identity and repository scope; task processes cannot select another project, receive the App private key, or fall back to the shared host GitHub login on refresh failure.
- **Board and control credentials stay central** — use the approved personal-board account credential only for registered board operations and a distinct trusted infrastructure credential for confirmed PR publication; neither is mounted in task or triage containers.
- **Project secret references, execution-time values** — configuration and operation reports contain 1Password references only; values are injected into the applicable execution with redaction and project isolation, requiring separation of writable runtime history from trusted credential storage.
- **Task-owned Git state and runtime history** — every task owns its writable checkouts/Git metadata, service data, and transcript/config state; a writable mount of the whole shared clone or runtime home cannot meet R18. Shared caches exclude secrets and mutable task data.
- **Compose is input, not authority** — resolve confirmed Compose files and services into a task-scoped service plan with owned networks, volumes, and a combined resource envelope; incompatible host mounts, sibling-task access, or privileged host control are rejected instead of weakening the sandbox.
- **Combined resource accounting** — CPU/memory limits apply to the whole task environment, not separately to each agent or Compose service; the rootless runtime mechanism must pass the resource and isolation scenarios before declaring this capability supported.
- **Keep parked data, stop execution** — stop agent/service execution as the park lifecycle releases its slot, retain owned volumes, and restart only through admission; cleanup follows existing terminal-retention policy and operates on exact ownership, not bare issue stems.
- **Repository-specific setup, project-level task bundle** — per-repository setup/check commands and workspaces feed one task and its review; no duplicate capacity charge per checkout. The repository-scope declaration and committed artifact ownership questions below remain explicit rather than silently picking a repository.
- **Required PR set is authoritative** — persist each required PR with its repository, head and own feedback/CI/conflict cursors; completing one cannot complete the task, and unknown or failed reads cannot be interpreted as all PRs merged.
- **Dependencies guide manual merges** — store explicit prerequisite edges within the task's required PR set, reject self/cyclic/dangling dependency declarations as invalid, and display dependency readiness separately from checks and review; this does not claim to prevent a human merge outside agent-ops.
- **Partial merge asks the operator** — when a required PR closes unmerged after another merged, retain the full PR state, park once for that incident, and deliver guidance requests through the existing operator surfaces; never auto-revert or infer permission to remove a required PR.
- **Preserve existing model and loop ownership** — project models use the effective existing box policy unless overridden, and provider admission still applies; loop counters remain owned by `dispatcher/loops.py`, with CI-fix attempts counted independently for each repository-qualified PR and capped at three. Polling, commits, and resume do not reset the count; failed CI after attempt three parks for guidance without a fourth automated attempt. Review-feedback limits are not changed by this decision.
- **Cross-repository release ownership** — application changes belong in agent-ops; host services, images, provisioning, credentials integration, sweeper, and selected app revision belong in agent-ops-infra. Infrastructure publication must select a compatible application revision before enabling the new schema on the box.

Current seams informing these decisions include `dispatcher/config.py::Target`,
`dispatcher/state.py::TaskState`, `task_key`, and `archive_root`,
`dispatcher/board.py::Item` and `join`, `dispatcher/main.py::_claim_new` and
`_poll_prs`, `dispatcher/containers.py` mounts, `web/app.py`'s boot-time target
map, and the infrastructure updater/sweeper. The existing broad host GitHub
credential mount and title-based private-board fallback are not safe compatibility
paths for a new multi-repository project.

GitHub documents repository grants through its
[installation API](https://docs.github.com/en/rest/apps/installations#add-a-repository-to-an-app-installation),
subject to the authenticated user's repository administration rights. App tokens
can be restricted by repository and expire, so refresh belongs in the trusted
credential path rather than manual per-project setup; see
[installation token issuance](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app).
Board creation and item insertion are documented in the
[Projects GraphQL reference](https://docs.github.com/en/graphql/reference/projects).
These references establish available API primitives, not that the operator's
actual installation, permissions, private-board visibility, or approval rules
have passed verification. Tickets 07, 16, and 17 must prove those capabilities
using suitable test accounts/fixtures before migration.

Task-definition and exploration decisions:

- **Q1 — repository scope source (resolved):** use the repository set specified
  in the original task definition, or discover the required member repositories
  during task exploration. Record the resulting scope on the task; the trusted
  controller validates project membership before issuing execution grants.
  Widening that scope after exploration needs operator confirmation: a
  session that needs another member repository parks and asks, and an operator
  confirmation records a new scope revision before the controller issues
  credentials for it. There is no automatic late expansion; exploration output
  does not grant a session authority to issue credentials.
- **Q2 — artifact ownership (resolved):** the user selects the member repository
  that holds the task's spec/plan, either in the task definition or interactively
  when it is missing. Persist that selection through stage transitions and
  resume; never silently default to the first repository. Artifact references
  retain their repository identity across the task bundle.

- **Q3 — remaining PR guidance policy (resolved):** CI repairs are capped at
  three attempts per PR. Review-feedback rounds are also counted per
  repository-qualified PR, each with the existing cap. When a required PR closes
  unmerged, operator guidance can direct a replacement PR in the same repository,
  recorded as a new required-set revision; removing a required PR needs an
  explicit operator drop command, and the agent never removes one itself. After
  a PR exhausts its three CI-fix attempts, an operator reply starts one guided
  session; the counter stays exhausted, and a further CI failure parks again
  without an automated fix.

Remaining unresolved inputs, not new policies:

- **Q4 — runtime capability:** validate rootless Compose resource grouping,
  task-private Git/runtime state, and provider authentication refresh without
  writable shared transcripts or duplicated OAuth refresh lineages. Exact
  unsupported Compose capabilities must be reported; they are not implicitly allowed.
- **Q5 — authorization capability:** verify actual App grants, board access to
  private member issues, organization approval, secret references, and required
  writes. Exact App/1Password identities and least required permission grants
  are deployment inputs, not invented values in this design.

## Data model

These are logical records and constraints, not implementation code or a mandate
to introduce SQL. `PK`, `UNIQUE`, `NOT NULL`, `CHECK`, and `FK` below state the
invariants the persistence/configuration boundary must enforce. With existing
YAML/JSON storage they are validated together under the owning lock before
atomic publication. If a relational store is selected later, enforce them there
as actual constraints; never leave admission and ownership to UI checks alone.

| Record | Fields | Constraint | Why |
|---|---|---|---|
| ProjectConfig | project_key, name, board_id, board_owner, board_number, desired_enabled, models | PK project_key; NOT NULL nonempty name/board identity; UNIQUE board_id; CHECK desired_enabled is boolean; nonempty repository set; box config CHECK enabled count <= 10 | One board and operational choice per project |
| RepositoryConfig | repository_id, canonical_name, project_key, clone_path, worktree_root, setup/check/verify commands | PK repository_id; FK project_key; NOT NULL verified GitHub identity and distinct owned paths; UNIQUE canonical identity after GitHub resolution; one project owner; required gate command | URL aliases cannot register the same repo twice; one setup owner per repo |
| BoardSchema | project_key, board_id, field/option IDs | FK project_key; UNIQUE project board mapping; required IDs match their actual field types and intended options | Reject incompatible or stale schemas without replacing board data |
| IssueRef / BoardItem | repository_id, issue_number, issue_node_id, project_key, item_id | PK repository_id + issue_number; CHECK number > 0; UNIQUE GitHub node ID when available; FK repository membership; UNIQUE board_id + issue_node_id; trusted issue-to-item association | Same issue number/title across repositories cannot collide |
| TaskState | task_key = project_key + origin_repository_id + origin_issue_number; stage, park, slot, timestamps, runtime picks, task bundle | PK full task key; FK origin IssueRef and project; one active logical task per origin issue; existing stage/park validity; atomic terminal transition | One originating issue, one unfinished-task charge, many affected repositories |
| TaskRepository | task_key, repository_id, worktree, branch, scope_revision | PK task_key + repository_id; FK task/project/repository membership; UNIQUE writable checkout ownership; NOT NULL recorded scope revision; repository subset validated outside the session | Cross-repo execution without cross-project access; task definition or exploration supplies scope |
| TaskArtifact | task_key, artifact_id, repository_id or task-bundle location, path, published reference, content revision | PK task_key + artifact_id; FK task and optional TaskRepository; path contained in owned workspace; publication repo/head verified; review revisions tied to exact task | Spec/review content cannot cross tasks; user-selected publication repository |
| RequiredPR | task_key, repository_id, pr_number, node_id, head_sha, state, merged_at, feedback/check/conflict cursors, ci_fix_attempts, required_set_revision | PK task_key + repository_id + pr_number; FK TaskRepository; CHECK pr_number > 0; UNIQUE node_id within the task; cursor/head state scoped to this PR; CHECK 0 <= ci_fix_attempts <= 3; atomic attempt admission only below 3, deduplicated by execution attempt identity; required-set revision updated atomically | One PR's outcome cannot stand in for another |
| PRDependency | task_key, dependent PR ref, prerequisite PR ref | UNIQUE edge; both FKs to same task's RequiredPR set; CHECK dependent != prerequisite; cycle rejection on whole-set write | Show a valid partial order; no dangling or artificial dependencies |
| TaskCompletion / GuidanceIncident | task_key, required_set_revision, observed PR outcomes, guidance fingerprint | FK task and exact required-set revision; conditional terminal update only when all required PRs are merged; UNIQUE incident fingerprint per task/revision/outcome; unknown state fails completion | Repeated polls do not finish early or repeatedly ask the same question |
| SecretBinding | project_key, optional repository scope, purpose, 1Password reference | FK project and member repository when supplied; NOT NULL reference; UNIQUE binding identity; no secret value fields | Only project-approved references enter Git/state/reports |
| AppInstallation / ExecutionGrant | installation_id, repository_id, execution_id, granted scope, token expiry, credential reference | Repository grant FK membership; FK trusted execution identity; requested scope subset of recorded task scope; cache key includes installation/repository/permissions; expired tokens never used; no fallback to broader credential | Refresh cannot widen access or leak another execution's grants |
| BoardAuthorization / ControlAuthorization | purpose, account/installation identity, credential reference | Distinct purpose; NOT NULL trusted-only reference; board operations FK registered project; configuration operation FK confirmed revision | Central authority cannot be requested as arbitrary task credentials |
| OnboardingOperation | operation_id, project identity, confirmed plan revision, actor, base infrastructure SHA, phase, resource receipts, failure | PK operation_id; one active onboarding for a project/membership identity; UNIQUE confirmed logical operation; allowed phase transitions; CAS expected revision; values contain references only | Reconnect and retry continue the same work |
| OperationStep / ExternalReceipt | operation_id, step identity, intended resource identity, GitHub/resource ID, status | PK operation + step; FK operation; UNIQUE resource ownership; uncertain create outcome must be reconciled before another create | No duplicate board/PR/checkout after a lost response |
| ConfigurationChange | operation_id, project_key, requested state/config delta, base/head/merge/applied revisions, checks/approval outcome | FK confirmed operation; CAS source revision; preserve unrelated deltas; requested != applied until matching revision verified; operation status cannot authorize another config revision | GitHub merge and box application are separate facts |
| ReservationHold | project_key, operation_id, intended config revision, admission state | UNIQUE project active hold; FK pending enable operation; enabled projects + admitted distinct enable holds <= 10; acquired/converted/released under convergence lock; no timestamp-only expiry | Protect enable between GitHub publication and box application |
| UnfinishedAdmission | unfinished task set, effective enabled set, pending admitted holds | Under one lock, candidate post-state must satisfy unfinished count + empty reservations for enabled/held projects <= 10; new claims only for effectively enabled projects; one count per TaskState | Includes paused work, excludes extra PRs, preserves exclusive empty places |
| ExecutionAllocation | slot_id, owner_kind = task/triage/verification, owner_id, generation, runtime resource identity, status | UNIQUE occupied slot; UNIQUE live owner/generation; FK task or operation as appropriate; CHECK occupied allocations <= configured capacity; no unfinished TaskState for verification | All launch sites share the same execution limit |
| Sandbox / ServicePlan | execution identity, task key or verification operation, repo workspace set, confirmed Compose sources/services, resource envelope | FK execution and confirmed project definitions; positive aggregate CPU/memory; UNIQUE network/volume/container ownership; mounts restricted to owned resources; no host runtime-control socket | Compose cannot bypass sandbox boundaries or multiply the budget |
| RetainedTaskResources | task_key, owned checkout/volume identities, lifecycle | FK task; park releases live service allocation only after stop; retained data survives resume; deletion conditional on existing cleanup eligibility and exact owner | Cleanup cannot remove a sibling task or a still-running service |
| LegacyIdentityMap / MigrationProgress | old target, old issue, old repository, canonical task key, verified resources, credential consumers | Unique proven old-to-new mapping; refuse ambiguous association; idempotent migration steps; do not retire credentials while any live consumer remains | Expand/contract can preserve existing work and history |

Admission example: with three enabled projects, two empty and one holding eight
unfinished tasks, the eight occupied places plus two empty reservations exhaust
the ten-place pool. With two enabled projects and a paused project holding two
tasks, one enabled project can hold seven while the other's empty place remains
protected. Legacy excess is retained; these checks reject new admission rather
than deleting existing records.

The long-lived operation never holds `convergence.lock` while waiting for GitHub.
Its admitted enable hold participates in each short claim/application decision.
The updater revalidates the incoming configuration and hold ownership against
current task state before publishing it. A stale PR cannot silently create an
eleventh enabled project. Execution allocation likewise remains held until the
owned process/resources are confirmed stopped, rather than being released merely
because the CLI disconnects.

## Identities

Every covered identity below names its implementation tickets. Ticket numbers
refer to `.agent/tickets/`; this is the migration coverage contract, not a copy
of the red-team findings. New identity serialization belongs to one shared owner
and is not reconstructed independently from display strings.

- **I01 — configured target/repository/board mapping, canonical repository URL and clone roots:** covered by 01, 16, 18, 21; stable project identity with globally unique repository membership.
- **I02 — board item `number`/title joins, issue-to-item map, rank rows, candidate and cancellation sets:** covered by 02, 16; repository-qualified issues and verified node/item association.
- **I03 — task JSON contents and `task-<target>-<issue>.json`, legacy `task-<issue>.json`, load/save/delete/retirement:** covered by 01, 03, 21; full task identity and proven legacy aliases.
- **I04 — `messages/<target>-<issue>.jsonl`, delivery IDs/count maps and legacy message merging:** covered by 03, 04, 21; queue remains tied to originating issue across retries.
- **I05 — intent filenames/payloads, competing answer groups, reply/run/cancel/resume deduplication:** covered by 03, 04; full task identity plus independent intent identity.
- **I06 — `session-*`, `waiting-*`, `background-*`, `wake-blocked-*`, `herdr-status/` sidecars and all legacy variants:** covered by 03, 05, 21; record readers, stop-hook writers, and cleanup move together.
- **I07 — execution overrides, quarantine filenames/bodies, failure hash and blocker links:** covered by 03, 04, 21; failure deduplication includes originating repository, blocker identity remains repository-qualified.
- **I08 — archive hash, archived `task.json`, artifact index/content location, IDs and redirect URLs:** covered by 03, 04, 11, 21; task archive and artifact repository location are distinct identities.
- **I09 — worktree directories, branch names, provisioning sentinel/log, `.agent/task.json`, stage signal and repo paths:** covered by 05, 09, 11, 21; task-private Git state with per-repository checkout ownership.
- **I10 — session/setup container names, herdr workspace/tab labels and Sessions clone mapping:** covered by 05, 09, 10; one execution identity for a task bundle, not one identity per PR.
- **I11 — stop-hook body and branch guard, waitd route, conversation association, runtime home/transcript ownership:** covered by 05, 07, 08, 21; a session cannot select another task's identity or credentials.
- **I12 — snapshot names, legacy snapshot fallback, autopsy copies, flush and sweeper matches:** covered by 05, 10, 21; exact owned resources, no bare-number cleanup.
- **I13 — scalar `pr_number`, `ci_run_id`, head/feedback/check/conflict cursors and PR notification dedupe:** covered by 01, 12, 13, 21; repository-qualified per-PR records and task-level required set.
- **I14 — `events.jsonl` task fields, history filter, event-derived last-claim ordering:** covered by 03, 04, 06, 12; task events qualify repository while fairness aggregates by project.
- **I15 — task/review/artifact/terminal HTTP and browser routes, source lookup, action bodies:** covered by 04, 19; canonical task route includes repository, legacy route never guesses.
- **I16 — issue-only queue boost/next/ready/run, forced claim, Telegram attach/boost/next references:** covered by 02, 04; qualified reference required when a bare number is ambiguous.
- **I17 — Telegram park-message IDs, reply matching, notification labels and task links:** covered by 04, 12; reply remains tied to one canonical task.
- **I18 — web rank/description caches, admission/model maps, wake marker sets, SSE invalidation:** covered by 02, 04, 06, 19; project caches retain repository identity in rows, task caches use full key.
- **I19 — frontend query keys, route params, React card keys, drag payloads, queue mutations and review messages:** covered by 04, 19; two repositories' issue #12 stay separate everywhere.
- **I20 — triage repo enumeration, per-repo cursors, decision/prompt filenames, apply and board repair joins:** covered by 02, 07, 16, 21; enumerate all project members and retain repository-level scope while paused.
- **I21 — infrastructure configure-git clone/worktree enumeration, sweeper arguments and archived legacy files:** covered by 05, 18, 21; use project membership and canonical ownership, not issue suffix matching.
- **I22 — operator-request fingerprints, plan/answer revision checks and spec publication repository/branch:** covered by 03, 04, 11; exact task context, with content fingerprints still revision-specific.
- **I23 — new onboarding operation, resource receipt, control PR, reservation hold and verification identity:** covered by 14, 15, 17, 18, 20, 22; retries and confirmation revisions refer to the same operation.
- **I24 — new execution grants, secret references, Compose networks/volumes and required-PR dependency edges:** covered by 07, 08, 09, 10, 11, 12, 13; grant/resource ownership follows execution and PR edges follow task/repository identity.
- **I25 — provider usage caches, provider-priority/model-settings revisions, heartbeat, global budget/auth alerts, convergence lock:** out of scope for task rekeying because they are intentionally box-wide; 06, 10, 20 use the existing lock and shared limits without turning these into per-project identities.
- **I26 — GitHub node IDs and repository-qualified external IDs, provider conversation IDs, message UUIDs, content digest algorithms:** out of scope for renaming because external/stable identifiers must be retained; 03–05 and 11–13 update their owning task associations and use of digests.
- **I27 — rotated historical event files:** out of scope for destructive rewrite; 04 and 21 provide an unambiguous legacy reader/mapping and do not attribute an ambiguous record to a guessed repository.

## Seams

Tests drive public behavior through these named seams. New signatures are design
contracts, not implementation bodies. Test doubles may replace GitHub/SSH/
1Password/runtime transports, but acceptance tests assert observable state,
commands, sandbox isolation, and web output rather than private helper calls.

| ID | Seam | Status | Contract |
|---|---|---|---|
| CONFIG | `dispatcher.config.load_config(config_path)` | Existing, extended | Validate project/repository/board membership, legacy single-repo config, model inheritance and enabled count |
| BACKLOG | `GitHubClient.rank_rows(project)` and `candidates(project)` | Existing, extended | Return repository-qualified board candidates and refuse ambiguous joins/actions |
| STATE | `dispatcher.state` public save/load/load_all/archive/session/message-facing persistence operations | Existing, widened | Full task identity, deterministic legacy mapping, idempotent durable round trips |
| PASS | `dispatcher.main.run_pass(cfg, deps)` | Existing, extended | Observable claims, resumes, parks, PR outcomes, slots and reservations under one shared admission boundary |
| TASK_API | Task/queue HTTP routes plus browser task/review routes | Existing, widened | Canonical `/api/task/{project}/{repository_id}/{issue}` family, project/repository-qualified queue bodies; legacy routes only when unambiguous |
| SESSION | `Sessions.spawn_stage/resume/end` and `waitd.handle_ping` | Existing, widened | Exact task execution and stop notification, observable session names, owned snapshots and lifecycle |
| CREDENTIAL | Trusted `ExecutionCredentials.acquire/refresh(execution_id, repository_id, purpose)` | New | Caller identity bound to execution; reference-only configuration; scoped expiring credentials, never broader fallback |
| SANDBOX | `TaskSandbox.prepare/start/park/resume/cleanup(execution_ref)` | New | Task-owned checkouts/Compose/runtime state, combined limits, parked data and exact cleanup; invoked by existing Sessions/workspace owners |
| PROJECT_CLI | `agent-ops project add`, `enable`, `pause`, `status` | New | Interactive proposal and confirmation; status/resume of a durable operation; explicit SSH/approval/capacity/deployment outcomes |
| CONTROL | `ProjectControl.propose/confirm/status` and operation reconciliation | New | Same confirmed operation for CLI/web; PR checks/merge/deployment receipts and short-lived lock sections |
| PROJECT_API | `GET /api/projects`, confirmed project change requests, operation status | New | Tabs from configured projects including empty/paused ones, desired versus applied revision and blockers; existing web auth boundary retained |
| DEPLOY | Infrastructure updater and configuration verifier, invoked as deployment entry points | Existing, extended | Revalidate/publish compatible configuration, convert reservation holds, reload web project view, preserve live task sessions |
| MIGRATE | Explicit migration/check entry point and infrastructure worktree sweep entry point | Existing, extended | Rehearsable/idempotent old identity and credential migration, live-consumer protection, exact cleanup |

PROJECT_CLI and PROJECT_API are the operator surfaces; internal composition stays
behind CONTROL, SANDBOX, and CREDENTIAL. The qualified task API is a deliberate
contract change, with compatibility adapters restricted to proven old identities.

All application tickets finish with `make gate` green, plus focused acceptance
tests at their seam. Infrastructure tickets finish with the infrastructure
repository's documented `make test` green. Cross-repository tickets run both in
their respective checkouts. UI tickets also demonstrate browser behavior and
include the repository's required visual evidence when preparing their PRs.
Credential/runtime tests use owned disposable fixtures and recorded permissions;
they do not mutate the operator's live projects merely to validate this design.
