# Project onboarding and isolated multi-project execution — Spec

## Requirements

1. **Local interactive onboarding.** Provide `agent-ops project add` on the
   operator's computer, managing the box through SSH. Accept or prompt for the
   GitHub repositories and project name. Inspect discoverable configuration rather
   than asking the operator to transcribe it. Show proposed changes, access
   grants, secret references, verification writes, and supporting services for
   confirmation before applying them. One agent-ops project corresponds to one
   GitHub Projects board and contains one or more repositories. A repository can
   belong to only one project. Repeating onboarding for the
   same project resumes it; attempting to register that repository under another
   project name is rejected and identifies the existing project. Alternate URL
   spellings for the same GitHub repository do not bypass this uniqueness rule.
2. **Versioned deployment.** Record project configuration in the infrastructure
   repository's `targets.yaml` through a PR. Confirmation authorizes creating
   that PR, waiting for required checks, and automatically merging when branch
   rules permit. Wait for the box to deploy and verify the configuration. Failed
   checks or required external approval leave a visible, resumable blocker;
   neither direct pushes to main nor live-config edits substitute for this flow.
3. **Separate checkouts and project web tab.** Provision a separate clone and
   worktree storage for each project repository on the box, including the
   ranking and skill prerequisites. The
   agent-ops development target has a checkout separate from the running
   application checkout. A successfully onboarded project appears in its own web
   tab even with no tasks, and its tasks and controls are scoped to that project.
   Multiple repositories do not create separate project tabs or reservations.
4. **Dedicated board discovery and creation.** Detect committed backlog metadata
   and offer to reuse that board, select another board, or create a dedicated
   board shared by that project's repositories. Validate identity, access, and
   required field structure. A board containing frontend and backend issues is
   valid when both repositories belong to the project. A board with issues from
   repositories outside the confirmed project membership cannot be selected;
   offer another board or a new one without removing unrelated cards. Support
   personal and organization
   repository owners and board authorization appropriate to the selected owner.
5. **Non-destructive board preparation.** Add missing required fields and options
   after including them in the confirmation. Preserve existing fields, options,
   cards, and values. An incompatible field type or ambiguous mapping blocks
   onboarding with an explanation rather than silently replacing data. Prepare
   each repository's labels required by the existing triage and claim flow without
   treating every imported issue as automatically eligible.
6. **Backlog population.** Add missing existing open issues from every member
   repository to the project's board, preserve existing labels and board
   statuses, and arrange for future open issues from those repositories to be
   added. Repeating this operation does not duplicate
   cards. Triage remains responsible for preparing eligible work under existing
   rules; import alone does not mark all issues Ready or auto.
7. **Automatic repository credentials.** After initial GitHub App setup and
   authorization, onboarding manages repository access and automatically issued,
   refreshed repository-specific tokens. Sessions receive scoped access to the
   project repositories required for their task, including multiple repositories
   for a cross-repository task. App signing keys and the authority to issue other projects'
   tokens remain outside task sandboxes. Personal and organization repositories
   are supported; additional GitHub or administrator approval is guided and
   resumable rather than bypassed or requiring per-project manual token creation.
8. **Trusted board and infrastructure access.** Organization board access uses
   the authorized App arrangement where supported. For personal boards, a
   dispatcher-only account-level board credential may be reused from 1Password.
   Trusted board operations are restricted to registered projects; task sessions
   cannot obtain that credential. The dedicated infrastructure control service
   can submit and merge confirmed configuration PRs, with its credential also
   excluded from task sandboxes. Board isolation through the dispatcher must not
   be represented as GitHub enforcing a separate credential per board.
9. **Project secrets.** Detect declared secret requirements and guide selection
   or creation of 1Password entries. Store references in configuration. Expose
   each project's secret values only to its authorized project execution, never
   another project's sandbox, Git history, ordinary command output, or logs.
   Missing required secrets block verification with the missing reference named.
10. **Detected and verified execution setup.** Inspect each repository's configuration
    and scripts to propose dependency setup, checks, and application verification
    commands. Let the operator confirm or edit them and supply missing required
    commands. Support repository-defined Compose service definitions in the first
    version: detect and propose the relevant services, obtain confirmation, and
    verify compatibility with the task sandbox and combined resource budget.
    Verify the accepted setup on the box; missing or incompatible Compose service
    definitions or failed checks leave onboarding incomplete and the project
    paused, with the failing step visible. New projects inherit the box's model
    and usage policy unless an optional project override is supplied; provider
    usage limits continue to apply.
11. **GitHub write verification.** Include temporary resources in the proposed
    changes and perform the writes needed to establish the project's operation,
    including branch push and PR creation for every member repository, and project
    board updates. Remove temporary
    resources or close them where deletion is unavailable. Report cleanup
    failures. Read-only clone or API access alone is not a successful write
    verification, and failed required writes prevent a successful onboarding result.
12. **Resumable completion.** Preserve completed onboarding steps and identify
    pending, blocked, failed, and verified work. Repeating the same onboarding
    resumes without duplicating its project, board, repository clones,
    credentials, or infrastructure PR.
    Preserve partial resources rather than automatically deleting them. Report
    complete only after required setup, access, deployment, and web visibility
    checks succeed. Newly added projects remain paused throughout this flow.
13. **Project pause semantics.** New projects start paused. A paused project
    cannot claim new tasks, but its existing tasks may continue, resume, receive
    feedback fixes, and complete under ordinary execution limits. Paused projects
    still receive scheduled triage, which consumes shared execution capacity.
    Existing task eligibility and execution gates remain in force when enabled.
14. **PR-backed enable and pause controls.** Both the project's web tab and a
    terminal command can request enable or pause after confirmation. Both use the
    infrastructure PR, checks, automatic merge, and verified deployment flow.
    Show current effective state and pending change separately. The current
    state remains effective until the change deploys; a failed PR or deployment
    must not be presented as an applied state change. These controls must not
    maintain an overriding box-local enable/pause setting.
15. **Ten unfinished tasks.** Keep the box-wide unfinished-task limit at 10 for
    this slice. Running, parked, awaiting review, awaiting CI, and open-PR work
    count until terminal under existing task semantics, including work belonging
    to paused projects. New claims cannot increase the total beyond 10, including
    simultaneous attempts. Existing above-limit state is retained and drains
    without automatic cancellation; it admits no additional new tasks.
16. **Exclusive per-project reservations.** Each enabled project receives one
    exclusive unfinished-task place. The remaining places float among projects
    under the existing claim ordering and execution gates. An enabled project's
    first unfinished task occupies its reserved place; its additional unfinished
    tasks use shared places. Paused projects have no empty reservation; all their
    unfinished tasks still occupy the total and reduce available shared capacity.
    No project borrows an enabled project's empty reserved place. Reservations
    guarantee an unfinished-task place, not immediate CPU, memory, or model access.
    A project receives one reservation regardless of repository count. One task
    spanning several repositories and PRs counts as one unfinished task.
17. **Enable limits and reservation admission.** Any number of projects can be
    onboarded paused, but at most 10 can be enabled. Enabling an eleventh requires
    pausing another first. If existing tasks leave no room for a newly requested
    reservation, enabling waits visibly rather than promising an unavailable
    place, evicting work, or exceeding 10. Conflicting or simultaneous requests
    must not oversubscribe reservations or discard another confirmed change.
18. **Per-task sandbox isolation.** Run repository setup, dependency
    installation, agents, tests, and supporting services inside sandboxes on the
    box. Each task has its own checkout for every required repository, service
    instances, network, and writable
    task data; tasks cannot alter another task's checkout or test database or
    access another project's private secrets. The host performs checkout,
    scheduling, and credential management. Shared dependency caches are allowed
    only where they expose neither secrets nor mutable task data.
19. **Sandbox lifecycle and combined resource budget.** A task's agent and all
    supporting services across its repositories share one CPU/memory budget.
    Additional repositories or PRs do not multiply that budget. Onboarding verifies that the
    declared environment fits; extra resources require an explicit configuration
    change. Parking stops supporting services and releases their execution
    resources while retaining task data. Resume restarts services with that data.
    Existing task cleanup removes retained resources after completion; this
    feature introduces no new retention duration or automatic data eviction.
20. **Verification shares execution capacity.** Onboarding uses a temporary
    sandbox with the ordinary task resource limits and one normal execution slot.
    A full box leaves verification waiting visibly and resumably. Verification,
    live tasks, and triage cannot jointly exceed execution capacity. Verification
    does not consume an unfinished-task place; its temporary resources are
    cleaned up and failures are reported.
21. **Migrate existing projects.** Include existing projects such as
    `portfolio_eval` in the App and repository-specific credential migration.
    Verify new repository and board access before switching. Allow current
    sessions to finish safely; preserve their task state, artifacts, and data.
    Retire the shared repository credential only when no remaining consumer
    needs it. A failed migration verification preserves working access and
    reports the blocker. Preserve each existing single-repository project as a
    valid one-member project without losing task or PR references. Recheck the live state instead of assuming the
    investigation's historical task counts are still current.
22. **End-to-end multi-project acceptance.** Demonstrate onboarding agent-ops
    alongside portfolio_eval: separate checkouts, dedicated boards, scoped
    access, sandbox verification, deployed configuration, and two visible web
    tabs. agent-ops starts paused; after confirmed enabling and reservation
    admission, both projects can claim eligible work without consuming each
    other's reserved places or violating shared execution limits. Also verify a
    frontend/backend project with one shared board and a task producing PRs in
    both repositories; the single-repository example alone is not sufficient.
23. **Cross-repository task and PR lifecycle.** One task may change multiple
    repositories in its project, within one task sandbox containing their required
    checkouts and Compose services. Track every required PR with its repository,
    expose all PRs and their individual review/CI/merge state in the task view,
    and apply the relevant checks to each repository's changes. The task remains
    unfinished until all required changes complete; merging one required PR does
    not finish a task while another required PR is open or has not merged. A
    failure in one repository remains visible even when another repository's PR
    succeeds. If one required PR has merged and another is closed without merging,
    park the task and request operator guidance. Show the merged and unmerged
    changes; do not declare success or automatically revert the merged changes.
    The task continues to occupy one unfinished-task place while awaiting guidance.
    Existing single-repository, single-PR tasks remain supported.
24. **Repository-qualified issue and PR identity.** Issues and PRs retain their
    GitHub repository identity throughout board ranking, claiming, state,
    operator actions, review/CI polling, and presentation. Two member repositories
    can each have issue #12 or PR #7 without collision or an action being routed
    to the wrong repository. One cross-repository task has one task identity with
    its related repository-qualified PRs; it is not counted as a separate task
    for each PR. Repeated polling or onboarding does not duplicate that work.
25. **Explicit PR dependencies and manual merge ordering.** Record which required
    PRs depend on other required PRs and show that ordering with the task's PR
    states. A dependent PR is not ready for merge until its prerequisites have
    merged, even if its own checks pass. Independent PRs have no artificial order.
    All task PR merges remain manual in this version; being independent or having
    passing checks does not authorize automatic merging. This is separate from
    the confirmed automatic merging of infrastructure configuration PRs. Future
    task-PR automation is out of scope: its risk policy is not yet defined and
    must account for dependency readiness separately from risk. The behavior and
    operator guidance for partially completed multi-PR work must remain visible
    rather than treating the task as a single undifferentiated PR.

26. **Per-PR CI repair limit.** Allow at most three automated CI-fix attempts
    per repository-qualified PR. Each PR has its own counter, including PRs in
    the same task. Repeated polling, new commits, and session resume do not reset
    that counter. If CI still fails after the third attempt, stop automated CI
    fixes for that PR and request operator guidance under the existing parking
    flow. Keep the task unfinished; do not merge the PR automatically. This
    decision does not change review-feedback limits.

## Scenarios

### Scenario: S01 — local command proposes a complete onboarding (R1, R10)

- **Given** the operator's computer can reach the box and repository
  `example/analytics` has setup scripts, board metadata, and declared test services
- **When** the operator runs `agent-ops project add` and supplies name `analytics`
  and that repository
- **Then** the command discovers and proposes the relevant settings, allows edits,
  and shows configuration changes, access, services, secret references, and
  temporary write tests before applying them.

### Scenario: S02 — no confirmation makes no onboarding changes (R1)

- **Given** the proposal for `analytics` is displayed and not confirmed
- **When** the operator exits
- **Then** no project configuration, board changes, access grants, test writes,
  or project provisioning from that proposal are applied.

### Scenario: S03 — checked configuration reaches the box (R2, R3)

- **Given** onboarding is confirmed and all required infrastructure PR checks pass
- **When** branch rules permit merge and the updater deploys the revision
- **Then** the command verifies the versioned target configuration, separate
  target checkout, and an `analytics` tab with zero tasks and paused state.

### Scenario: S04 — failed checks or approval requirements block deployment (R2)

- **Given** the infrastructure PR has a failing required check or requires an
  organization approval that has not been granted
- **When** onboarding reaches publication
- **Then** it reports the specific blocker and PR, remains resumable, and does not
  bypass the check or approval by writing main or the live configuration.

### Scenario: S05 — development checkout does not replace runtime checkout (R3)

- **Given** the dispatcher runs from `/home/agent/agent-ops`
- **When** agent-ops is onboarded as a development target
- **Then** its clone and task worktrees use separate storage and task edits do not
  change the deployed runtime checkout.

### Scenario: S06 — select or create a dedicated board (R4)

- **Given** `analytics` has committed metadata for a compatible dedicated board
- **When** the operator accepts that board
- **Then** onboarding reuses its identity without creating another board.

### Scenario: S07 — repository without board metadata can be onboarded (R4)

- **Given** `example/analytics` has no committed board metadata
- **When** the operator chooses to create a board and confirms
- **Then** onboarding creates a dedicated board and records its verified identity
  and required field mappings in the project setup.

### Scenario: S08 — unrelated board cards are never removed (R4)

- **Given** the selected board has an issue from `example/billing`, which is not
  in the confirmed repository membership of project analytics
- **When** onboarding validates it for analytics
- **Then** selection is rejected with an explanation and an offer to choose or
  create another board, and the billing card remains unchanged.

### Scenario: S09 — add missing board structure without resetting data (R5)

- **Given** the selected dedicated board lacks a required numeric field, has
  existing statuses and cards, and the required automation labels are absent
- **When** the operator confirms the proposed additions
- **Then** missing structure and required labels are created while existing
  fields, options, card values, and issue labels remain intact.

### Scenario: S10 — incompatible field mapping blocks onboarding (R5)

- **Given** the selected board has a text field where a required numeric field is
  expected, or two existing fields cannot be mapped unambiguously
- **When** onboarding validates the board
- **Then** it identifies the incompatibility, does not replace or guess the
  field, and waits for resolution.

### Scenario: S11 — existing and future issues enter the board once (R6)

- **Given** open issues #1 and #2 exist, #1 already has a board card marked Ready,
  and #2 lacks the auto label
- **When** import runs twice and open issue #3 is later created
- **Then** each issue has one card, #1 retains its status, #2 is not made auto by
  import, and #3 is added through the configured future-issue mechanism.

### Scenario: S12 — tokens are issued and refreshed for one repository (R7)

- **Given** the App is authorized for analytics and billing and an analytics
  session is running
- **When** its repository token needs replacement
- **Then** replacement is automatic and remains restricted to analytics, without
  a new manually created per-project token or access to the App private key.

### Scenario: S13 — cross-project credential use is denied (R7, R8, R9)

- **Given** an analytics task has its own repository token and project secrets
- **When** it attempts to write to private repository billing, read billing's
  secret, obtain the board credential, or obtain the infrastructure credential
- **Then** none of those accesses is granted through the task's credentials,
  mounts, or credential service.

### Scenario: S14 — organization approval can be resumed (R7, R12)

- **Given** onboarding targets an organization repository whose administrator has
  not approved the required App installation
- **When** onboarding reaches that authorization step
- **Then** it explains the required action and remains incomplete; after approval,
  rerunning resumes with the same project and resources.

### Scenario: S15 — personal boards reuse trusted account authorization (R8)

- **Given** analytics and billing use different personal-account boards and an
  account-level board credential is already configured in 1Password
- **When** billing is onboarded and its board access is verified
- **Then** the dispatcher can reuse that credential without giving it to billing
  tasks, and refuses an operation targeting an unregistered board.

### Scenario: S16 — required secret is missing (R9, R10)

- **Given** analytics declares a private registry credential and the selected
  1Password reference is unavailable
- **When** setup verification starts
- **Then** onboarding names the missing reference, remains incomplete and paused,
  and neither prints a secret value nor substitutes another project's credential.

### Scenario: S17 — confirmed commands and service definitions are verified (R10)

- **Given** detection proposes setup and gate commands and PostgreSQL and Redis
  test services declared in `compose.yaml`, and the operator edits the gate command
- **When** the operator confirms and verification runs
- **Then** the edited command runs with the declared service in a temporary box
  sandbox, and results refer to the commands actually executed.

### Scenario: S18 — missing service definitions or failing checks are visible (R10)

- **Given** setup requires PostgreSQL but has no usable Compose definition, or
  its confirmed gate command fails
- **When** onboarding verifies execution readiness
- **Then** it reports the missing definition or failed check and leaves the
  project paused and onboarding incomplete rather than claiming readiness.

### Scenario: S19 — inherited and overridden model policies retain usage gates (R10)

- **Given** analytics has no model override and billing has a valid project override
- **When** either project requests execution
- **Then** analytics uses the effective box policy, billing uses its override,
  and both remain subject to applicable provider usage limits.

### Scenario: S20 — writes are proved and temporary resources are cleaned (R11)

- **Given** confirmation includes temporary branch, PR, and board write tests
- **When** those operations succeed for analytics
- **Then** onboarding records successful write verification, removes or closes
  the temporary resources as supported, and reports any failed cleanup.

### Scenario: S21 — readable repository does not prove write access (R11)

- **Given** the box can clone analytics but cannot push the verification branch
- **When** onboarding tests writes
- **Then** it reports the denied operation and does not report successful
  onboarding based on clone access or a repository permissions summary.

### Scenario: S22 — interruption preserves resources without duplicates (R12)

- **Given** onboarding has created one board and one infrastructure PR and then
  loses its SSH connection
- **When** the operator reruns onboarding for the same project and repository
- **Then** it resumes verified progress, reuses the board and PR, creates no
  duplicate project or credentials, and reports complete only after all checks.

### Scenario: S23 — paused project remains visible without claiming work (R3, R13)

- **Given** analytics is successfully onboarded paused and has a Ready auto issue
- **When** the web loads and the dispatcher runs with a free execution slot
- **Then** analytics has its own tab showing paused state and the dispatcher does
  not claim the new issue.

### Scenario: S24 — pause allows existing work and scheduled triage (R13)

- **Given** paused analytics has an existing task with an operator reply, a due
  triage sweep, and a new eligible issue
- **When** execution capacity and usage allowance permit
- **Then** existing work can resume, triage can run within shared capacity, and
  the new issue remains unclaimed.

### Scenario: S25 — enable and pause show deployment progress (R14)

- **Given** analytics is enabled and the operator confirms Pause in its web tab
- **When** the control service creates the PR and waits for checks and deployment
- **Then** the web and terminal show enabled as effective and pause as pending;
  after verified deployment both show paused, and new claims stop.

### Scenario: S26 — failed control changes leave effective state unchanged (R14)

- **Given** analytics is paused and an Enable request from the terminal has a
  failed PR check or deployment
- **When** the web reads its state
- **Then** it shows paused with the failed or blocked enable request, not enabled,
  and no local override makes the dispatcher claim new analytics tasks.

### Scenario: S27 — the tenth unfinished task fits but the eleventh does not (R15)

- **Given** one enabled project has 9 unfinished tasks, another eligible issue,
  and sufficient execution capacity and usage allowance
- **When** new claims are attempted
- **Then** one new task can bring the total to 10, and a further claim is denied.

### Scenario: S28 — simultaneous claims cannot exceed the total (R15)

- **Given** there are 9 unfinished tasks and two simultaneous admissible claim attempts
- **When** both attempt to occupy the remaining place
- **Then** at most one new task is created and the unfinished total is at most 10.

### Scenario: S29 — legacy excess drains without deletion (R15, R21)

- **Given** migration discovers 13 unfinished portfolio_eval tasks with a limit of 10
- **When** new claim rounds run and existing tasks complete
- **Then** existing work and state remain intact, no new claims occur while the
  total is 10 or greater, and claims resume only when both total capacity and
  reservation constraints permit.

### Scenario: S30 — shared places stop at other projects' reservations (R16)

- **Given** analytics, billing, and catalog are enabled, analytics has 7
  unfinished tasks, and billing and catalog each have none
- **When** analytics requests two more tasks with sufficient execution capacity
- **Then** its eighth task is allowed and its ninth is denied, preserving one
  empty place each for billing and catalog.

### Scenario: S31 — both empty reservations remain usable (R16)

- **Given** the same three enabled projects and analytics has 8 unfinished tasks
- **When** billing and catalog each submit their first eligible task as execution
  capacity becomes available
- **Then** both can claim one, bringing the total to 10 without borrowing.

### Scenario: S32 — paused work counts without an empty reservation (R15, R16)

- **Given** analytics and billing are enabled, catalog is paused with 2 unfinished
  tasks, billing has none, and analytics has 6
- **When** analytics requests two additional tasks
- **Then** its seventh is allowed and its eighth is denied: catalog's 2 tasks
  still count and billing's empty reserved place remains protected.

### Scenario: S33 — reservation is not an execution slot (R16, R20)

- **Given** billing has an unused reserved place but both execution slots on a
  capacity-2 box are occupied
- **When** billing has an eligible issue
- **Then** billing waits for execution capacity; its reservation does not permit
  a third live task or bypass provider usage limits.

### Scenario: S34 — the tenth enabled project fits (R17)

- **Given** 9 projects are enabled, each has at most one unfinished task, and a
  tenth verified project is paused
- **When** its confirmed enable change deploys
- **Then** 10 projects may be enabled with one reserved place each and no shared places.

### Scenario: S35 — an eleventh project can be onboarded but not enabled (R17)

- **Given** 10 projects are already enabled
- **When** an eleventh project is onboarded and Enable is requested
- **Then** onboarding can complete paused, but enabling requires pausing another
  project and does not silently remove an existing reservation.

### Scenario: S36 — new reservation waits for existing work to drain (R17)

- **Given** analytics is the only enabled project and has 10 unfinished tasks,
  and verified billing is paused with none
- **When** the operator confirms enabling billing
- **Then** the request visibly waits for reservation capacity without canceling
  analytics work; once one task completes, the request can complete through the
  PR/deployment flow without another claim stealing billing's admitted reservation.

### Scenario: S37 — simultaneous enables cannot promise eleven reservations (R17)

- **Given** 9 projects are enabled and two paused projects request enabling
- **When** their configuration changes are checked, merged, and applied
- **Then** no resulting effective configuration enables 11 projects; one request
  remains blocked with the reason visible rather than discarding the other change.

### Scenario: S38 — task sandboxes isolate repository and service state (R18)

- **Given** two analytics tasks and one billing task run their own database services
- **When** analytics task #1 edits its checkout and changes its test database
- **Then** analytics task #2 and billing retain their own checkout and database
  contents, and neither is reachable through task #1's private sandbox network.

### Scenario: S39 — repository execution stays inside the sandbox (R18)

- **Given** analytics has a dependency install command, gate command, and Redis service
- **When** onboarding or task execution runs them
- **Then** each runs inside its assigned box sandbox; a request requiring host
  execution or access to another task's private data is not silently granted.

### Scenario: S40 — shared cache cannot leak task-private content (R18)

- **Given** analytics and billing use a shared dependency cache
- **When** analytics writes a task-private file or receives a project secret
- **Then** that content is not exposed through the cache to billing.

### Scenario: S41 — agent and service resources share one limit (R19)

- **Given** a verification fixture configures a combined task allowance of 2 GiB
  of memory and 2 CPUs for an agent and database
- **When** verification runs or their combined demand exceeds that allowance
- **Then** both are accounted within that single allowance, excess resources are
  not silently granted, and inability to run successfully within it is reported.

### Scenario: S42 — park and resume preserve data while releasing services (R19)

- **Given** analytics task #12 has a database containing test record `invoice-12`
- **When** it parks and later resumes with capacity available
- **Then** its supporting services stop while parked, its stored record remains,
  and restarted services on resume see the same record.

### Scenario: S43 — task cleanup removes retained sandbox resources (R19)

- **Given** analytics task #12 is terminal and eligible under existing task cleanup
- **When** that cleanup runs
- **Then** its retained service resources and task data are removed without
  removing another task's data or introducing a new retention period.

### Scenario: S44 — verification waits for a shared slot (R20)

- **Given** box execution capacity is 2 and two task or triage slots are occupied
- **When** analytics onboarding reaches verification
- **Then** it shows waiting for capacity, starts no extra sandbox, and can resume
  when a normal slot is available.

### Scenario: S45 — verification occupies a slot but not an unfinished place (R20)

- **Given** the box has 10 unfinished tasks, 1 of 2 execution slots is occupied,
  and onboarding verification is pending
- **When** verification is admitted to the free slot
- **Then** the unfinished-task count stays 10, both execution slots are occupied,
  and no task or triage session can take a third slot.

### Scenario: S46 — temporary verification failure releases its resources (R12, R20)

- **Given** onboarding verification holds a slot and its temporary database setup fails
- **When** the failure is handled
- **Then** its execution slot is released, temporary sandbox cleanup is attempted
  with any failures reported, and the project remains paused and resumable.

### Scenario: S47 — migrate access without interrupting existing work (R21)

- **Given** portfolio_eval has an existing session using the legacy credential
  and the new App's repository and board access tests pass
- **When** migration proceeds
- **Then** the old session can finish safely, later sessions use scoped access,
  state and artifacts survive, and the shared repository credential is retired
  only after its last consumer no longer needs it.

### Scenario: S48 — failed credential migration preserves working access (R21)

- **Given** portfolio_eval works with its existing credentials but the proposed
  replacement fails a required repository or board write
- **When** migration validates replacement access
- **Then** it reports the failure and does not revoke working credentials or
  claim that migration completed.

### Scenario: S49 — onboard agent-ops beside portfolio_eval (R22)

- **Given** portfolio_eval is already configured and agent-ops has eligible board
  issues but is not yet onboarded
- **When** the operator confirms agent-ops onboarding and all checks and deployment succeed
- **Then** both web tabs and separate target checkouts exist, agent-ops is paused,
  each project has its dedicated board and scoped session access, and the box's
  running application checkout remains separate.

### Scenario: S50 — both projects can claim after enabling (R22)

- **Given** the two projects are verified and enabled, each has one eligible issue,
  no unfinished tasks, two execution slots, and sufficient provider allowance
- **When** the dispatcher claims work
- **Then** each project can receive one task in its own sandbox, both tasks count
  toward the total of 10, and their services and credentials remain isolated.

### Scenario: S51 — a repository cannot be registered as a second project (R1, R12)

- **Given** `example/analytics` is already registered as project `analytics`
- **When** the operator attempts to add the same repository as `analytics-copy`,
  including through an alternate HTTPS or SSH URL for that repository
- **Then** onboarding identifies the existing project and rejects the duplicate
  before creating another board, checkout, credential setup, or configuration PR;
  repeating onboarding as `analytics` instead resumes the existing project.

### Scenario: S52 — frontend and backend share one project board (R1, R3, R4, R6, R22)

- **Given** project `shop` includes `example/shop-frontend` and
  `example/shop-backend` and the selected board contains issues from both
- **When** confirmed onboarding verifies and deploys shop
- **Then** the board is accepted, both repository checkouts and their prerequisites
  exist, open issues from both feed that board, and the web shows one paused shop
  tab rather than separate frontend and backend projects.

### Scenario: S53 — repository count does not increase reservations (R16)

- **Given** shop has 2 repositories and analytics has 1, both projects are enabled,
  and neither has unfinished tasks
- **When** admission capacity is calculated
- **Then** there are 2 exclusive project places and 8 shared places, not 3 project
  places or a separate allocation for each repository.

### Scenario: S54 — a cross-repository task creates multiple PRs (R7, R18, R19, R22, R23)

- **Given** a shop task requires a backend API change and a frontend change
- **When** it runs with sufficient capacity and provider allowance
- **Then** one task sandbox has the required frontend and backend checkouts,
  scoped credentials for both, and verified Compose services under one combined
  resource budget; the task can create a PR in each repository and its view
  shows both with their respective check results.

### Scenario: S55 — one merged PR does not complete a multi-PR task (R15, R16, R23)

- **Given** one shop task has required frontend and backend PRs and occupies one
  unfinished-task place
- **When** the backend PR merges but the frontend PR remains open with failing CI
- **Then** the backend merge and frontend failure remain visible, the task is
  unfinished, and it still occupies exactly one place; it cannot complete until
  the remaining required changes complete.

### Scenario: S56 — all required PRs complete the same task (R23, R24)

- **Given** a shop task's required backend PR has merged and its frontend PR is
  the last required change still open
- **When** the frontend checks pass and its PR merges, and lifecycle polling repeats
- **Then** the task reaches done once, retains links to both repository-qualified
  PRs, and releases one unfinished-task place rather than creating duplicate tasks.

### Scenario: S57 — identical issue numbers remain different issues (R6, R24)

- **Given** shop's board contains `example/shop-frontend#12` and
  `example/shop-backend#12`, each independently eligible
- **When** ranking, claiming, and an operator action address the frontend issue
- **Then** both issues retain distinct identities and the backend issue's state
  and messages are not overwritten or acted upon as the frontend issue.

### Scenario: S58 — identical PR numbers retain separate lifecycle state (R23, R24)

- **Given** a shop task has frontend PR #7 and backend PR #7
- **When** polling observes frontend PR #7 merged and backend PR #7 still open
- **Then** the two repository-qualified PR states remain distinct and the task
  does not become done from the frontend result alone.

### Scenario: S59 — incompatible Compose definitions do not weaken isolation (R10, R18)

- **Given** the confirmed Compose service definition requires access to another
  task's private volume or a host resource outside the task sandbox
- **When** onboarding verifies Compose compatibility
- **Then** it reports the incompatible requirement, leaves onboarding incomplete
  and the project paused, and does not grant the extra access to make setup pass.

### Scenario: S60 — every member repository must pass access verification (R11, R12)

- **Given** shop's frontend repository passes its write checks but the backend
  repository denies PR creation
- **When** onboarding verifies the project
- **Then** it names the backend access failure, retains frontend progress for
  retry, and does not report the two-repository project successfully onboarded.

### Scenario: S61 — a partially merged task requests guidance (R23)

- **Given** a shop task requires backend PR #7 and frontend PR #9, and backend
  PR #7 has merged
- **When** frontend PR #9 is closed without merging
- **Then** the task parks and requests operator guidance, shows backend PR #7 as
  merged and frontend PR #9 as closed without merge, retains one unfinished-task
  place, and neither declares success nor automatically reverts backend PR #7.

### Scenario: S62 — passing checks do not satisfy an unmerged prerequisite (R25)

- **Given** frontend PR #9 depends on backend PR #7, both have passing checks,
  and backend PR #7 awaits a manual merge
- **When** the task view presents the required PRs
- **Then** frontend PR #9 is shown waiting for backend PR #7 to merge, and neither
  PR is automatically merged by the task pipeline.

### Scenario: S63 — a merged prerequisite makes its dependent eligible in order (R25)

- **Given** frontend PR #9 depends on backend PR #7 and frontend PR #9 passes its
  applicable checks
- **When** backend PR #7 is manually merged
- **Then** the frontend PR's dependency is satisfied and the task view reflects
  that it can proceed through manual merging, without merging it automatically
  or declaring the whole task complete before its remaining changes finish.

### Scenario: S64 — independent PRs do not acquire an artificial dependency (R25)

- **Given** backend PR #7 and frontend PR #9 belong to the same task but neither
  depends on the other and both pass their applicable checks
- **When** backend PR #7 remains open awaiting the operator
- **Then** frontend PR #9 is not shown blocked solely by membership in the same
  task; either can be manually merged first and both are still required for completion.

### Scenario: S65 — CI repair allowances are independent per PR (R26)

- **Given** frontend PR #7 has used two CI-fix attempts and backend PR #7 has used none in the same task
- **When** both PRs require another CI fix
- **Then** frontend can receive its third attempt and backend its first; the repository-qualified counters remain independent.

### Scenario: S66 — no fourth automated CI-fix attempt (R26)

- **Given** frontend PR #7 has used three CI-fix attempts and CI still fails
- **When** polling repeats or the task session resumes with another commit
- **Then** no fourth automated CI-fix attempt starts, the exhausted allowance is visible, and the task parks for operator guidance while remaining unfinished; the PR is not automatically merged.

## Out of scope

Repositories belonging to multiple agent-ops projects; boards shared between
separate agent-ops projects or destructively repurposed boards; supporting-service
definition formats other than Compose in the first version; automatic
task claiming upon onboarding; host-side repository execution; task access to
central credentials or another project's secrets; borrowing reserved places;
more than 10 enabled projects or 10 newly admitted unfinished tasks; implicit
resource increases; direct main/live-config writes; bypassing approvals or checks;
automatic cancellation or destructive rollback; new retention policies; and
project removal/renaming workflows; automatic merging or risk classification of
task PRs; and automatic rollback of partially merged task changes. Confirmed
automatic merging of infrastructure configuration PRs remains in scope. See
[proposal.md](proposal.md) for rationale.
