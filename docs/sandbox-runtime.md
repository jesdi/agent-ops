# Box sandbox runtime contract

Project repository configuration carries the operator-confirmed `compose_services`
and the combined `sandbox_memory` (bytes) and `sandbox_cpus` allowance. Project
claims provision through `TaskSandbox`; legacy `targets` retain their runtime for
migration. An explicit `sandbox: true` also selects this path for a legacy target.

The application calls the infrastructure-provisioned `agent-ops-sandbox`
executable. `AGENT_OPS_SANDBOX_RUNTIME` may select another trusted executable.
Missing runtime support blocks execution; the application never substitutes box
shell execution or a writable shared clone mount.

Lifecycle calls have the form `agent-ops-sandbox OPERATION [OPTIONS]` and receive
one JSON serialization of `ServicePlan` on stdin:

- `create`: idempotently provision the plan's resource group, private networks,
  owned volumes and containers. Enforce the combined CPU and memory allowance
  before any agent or service starts. The agent uses the deployment's agent
  image and waits for setup or an interactive session.
- `start --container NAME`: start a container and wait for its declared health
  check. Failure must return nonzero. Service names resolve only within the
  plan's private network.
- `exec --container NAME --command COMMAND`: execute the confirmed command in
  the agent's private checkout. Return JSON with `exit_code` and optional
  `output`; a command failure is a result, while a runtime failure exits nonzero.
- `stop --container NAME`: idempotently stop a container, releasing live resources
  while retaining its volumes and checkout.
- `remove`: idempotently remove every owned runtime resource, including partial
  creations. Failure returns nonzero and keeps the application's ownership
  record available for retry.

Lifecycle operations must reject resources outside the supplied plan. Command
output and project environment values must never be copied to runtime logs.
Environment values arrive through the process environment, separate from the
plan and command arguments. The application redacts project secrets from setup
failure reports.

Interactive sessions use `agent-ops-sandbox session --plan PATH -- LAUNCHER...`.
The trusted plan lives outside the task checkout. The helper must serialize with
other lifecycle operations, replace the idle agent with the supplied interactive
launcher, and retain the services, volumes, private network and resource group.
The launcher uses the plan's agent name, allocated caps, network and cgroup
parent; it mounts the private checkout and execution-specific runtime state.
It does not mount the shared clone. Validate these settings against the durable
plan before running the launcher, and fail closed on unsupported settings.

The application serializes prepare/start/park/resume/cleanup with a per-execution
file lock under `locks/<digest>`. Plans live under `plans/<digest>.json`, where
`digest` is `t` followed by the first 16 hexadecimal digits of SHA-256 of the
execution reference. The session helper must use that same lock when replacing
the agent. A retry reuses the persisted plan; editing Compose in the shared clone
does not replace ownership of resources retained by an existing execution.

The controller may establish an initially unspecified repository scope once,
at the spec-to-plan handover after ending exploration. It validates project
membership before requesting credentials, computes the combined allocation,
and stops the old containers. It then calls `remove` with only those containers
and the resource group, leaving the plan's network and volume lists empty so
their data survives. The new durable plan retains the origin checkout and owned
volumes and adds the required member checkouts and services. Normal prepare and
resume calls never expand scope; subsequent expansion requires operator policy.

This executable and box-level enforcement belong to the deferred infrastructure
part of ticket 09. Application tests exercise this transport with a fake runtime;
they do not establish box-level isolation or resource enforcement.
