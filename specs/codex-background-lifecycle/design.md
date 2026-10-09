# Bound turns and background results — Design

The accepted Stage 2 plan supersedes Stage 1's reference to Codex Stop as authority:
Codex `turn/started` and normal `turn/completed` from the controlled app-server
are authoritative. Legacy notify and native child notifications cannot end a main turn.
Claude uses native session and prompt identities, with a fresh physical launch fence.

## Ownership and identity

The host wait listener alone writes version-1 runtime snapshots. The dispatcher
alone writes task state. Every physical launch, including a resume, receives a UUID.
A binding contains target, issue, stage, ticket, launch_id, runtime, worktree and
conversation_id. A conversation can be bound once; resumed conversations are explicit.
The snapshot is outside every container mount; only the existing wait socket directory
is shared, read-only inside every task container, including Claude, Codex, and sessions
granted a secondary provider. The host listener retains directory write access to create
or replace its socket; native hooks and the controller only connect to that existing
socket. A writable directory would let a task replace the listener endpoint and intercept
the host authorization header even without access to its credential file. Read-only
directory binding prevents that replacement while retaining the Unix socket connection
path. T8 must verify actual Podman enforcement and socket connection behavior.
All session, setup and triage bind sources are validated before a command is returned.
Canonical source paths cannot contain or sit inside the private `runtime` store or
`runtime-host-token`, even through a read-only bind or symlink alias. This includes
the worktree and its `.git`-selected clone, primary and granted provider homes/binaries,
supervisor source/dependencies, and supporting configuration mounts. Writable aliases
of the wait directory are also rejected. Missing paths reserve the private locations
before creation; unresolvable paths fail closed. Provider homes remain shareable when
separate from private runtime state. Unsafe configured layouts must be relocated by
the operator before launch; command construction does not move existing state.
`Sessions(state_dir=...)` owns the launch directory: preparation, the container's
client environment, read-only wait mount, provider homes and every bind's validation
use that same directory. `podman_cmd` and `session_cmd` accept an optional keyword
`state_dir`; direct callers that omit it retain the environment/default selection.
Relative session state is made absolute at construction, and launch worktrees before
binding or prompt creation. Every host bind emits the canonical absolute source that
was validated, so a later terminal working directory or symlink alias cannot select
another source. Container destinations preserve useful absolute aliases; relative
worktree, clone and wait destinations become absolute before command construction.
Listener requests are serialized and snapshot replacement is atomic and
fsynced. The runtime_snapshots store keeps one complete document per launch under a
hashed task directory, with an atomic current.json pointer. Missing or malformed documents
behind an existing pointer remain managed unknown; an existing task runtime directory also
retains managed ownership when its pointer is lost or preparation was interrupted before
pointer publication. Obsolete launch IDs cannot select current state. Invalid/unreadable versions yield unknown views, never empty inventory.

Main turns carry an ID, status and seen-turn identities. Commands are identified by
owning thread and initial command item; descendants by child thread and turn. Ancestry
must reach the bound root at every depth. Inventory certainty is explicit. Outcomes,
batches, sent-unconfirmed receipts, confirmed receipts, history checkpoint, wait clock
and pending alerts survive restart. A missing worker is unknown until an outcome exists.

## RuntimeControl boundary

`dispatcher.runtime_control.RuntimeControl(state_dir)` is the listener's state boundary.
Public methods: `prepare(target, issue, stage, *, ticket='', runtime='codex',
conversation_id=None, worktree='') -> dict`; `view(target, issue,
launch_id=None) -> dict | None`; `event(binding, event, *, now=None) -> bool`;
`retire(binding, revision, *, reason='stopped', now=None, cap=10800) -> str`;
`accept_input(binding, client_message_id) -> bool`.

A prepared snapshot exposes `binding`, `revision`, `service`, `main`, `inventory`,
`workers`, `completions`, `deliveries`, `wait`, `alerts`, and `retired`. The prepared
binding is the returned snapshot's `binding`. Main status is unknown/active/stopped;
service is unknown/live/dead; inventory is unknown/known. Event types initially are
`bound` (conversation_id), `turn/started` (thread_id, turn_id), `turn/completed`
(thread_id, turn_id, status=completed), and `service` (status).

`RuntimeClient(state_dir)` exposes the same preparation/view/event contract over the
existing Unix HTTP listener. Host-only prepare requests require a listener-owned
credential outside the mounted wait directory; container events require their exact
prepared launch binding. Socket view requests need either the host credential or the exact
current launch ID. RuntimeClient is the host transport (re-exported by runtime_control);
its view reads atomic snapshots without mutating them. A listener restart preserves snapshots. A managed unreadable
snapshot remains a managed unknown view at the Sessions boundary.

The public exact-launch read wire is `POST /runtime/view` on
`<state_dir>/wait/wait.sock`, with JSON `{target, issue, launch_id}` and
`Content-Type: application/json`. An exact current launch ID needs no host
credential header. HTTP 200 returns the snapshot, null for a foreign/replaced
launch, or a managed-unknown view for damaged state. Container event requests
use `POST /runtime/event` with JSON `{binding, event, now?}` and return a JSON
boolean; after an accepted initial `bound` event, clients read the updated
binding before sending later events.

The container's `BoundClient` uses those two HTTP routes and never reads the
host credential or snapshot files. A `control/unknown` event carries a nonempty
`message`, invalidates main/inventory certainty while preserving seen-turn IDs
and the wait clock, and records a deduplicated `{kind: 'compatibility', message}`
alert. The listener records an accepted normal control completion as a
`SessionRecord` using the binding's conversation and stage. Late task-state
writes and supplementary native Codex hooks cannot select that record's stage.

A reconnected controller uses exact-root `thread/resume` to subscribe to future
turn notifications: `thread/read` plus inventory does not subscribe on 0.156.1.
An ordered response callback applies an authoritative active resume snapshot
before subsequent wire notifications. Its `turn/recovered` event requires
`status: 'inProgress'` and the bound thread/turn identity; it can restore the
latest seen turn from unknown or establish a new unseen turn, never an older
seen turn. Historical completed-turn/result reconciliation remains T7's scope.

Terminal attachment can proceed after the initial RPC reply or an accepted bound
`turn/started` while that request is outstanding. If both are lost, exact-root
resume must expose the initial prompt's launch-specific `clientUserMessageId`
as a `userMessage.clientId` in valid turn history before attachment. A matching
completed, failed, or interrupted turn permits attachment only; it does not
synthesize a normal Stop or SessionRecord. A later current turn cannot hide that
initial receipt. Recovery never repeats the initial prompt, and unrelated history
cannot release initial terminal readiness. Until that receipt is visible, the
controller rechecks the exact root at its inventory polling interval.



For Claude, SessionStart establishes service health only for the explicitly selected
conversation. UserPromptSubmit maps native session_id/prompt_id to main thread/turn.
Stop maps to normal completion and atomically reports its background_tasks in that same
snapshot; missing/unreadable background inventory is unknown. Child agent_id callbacks
cannot end the main turn. `turn/completed` events for runtime Claude accept a
`background_tasks` list; its first nonempty stopped report starts wait.since, and new
work alone resets it. Native hooks obtain launch identity from immutable launch
environment, never a mutable worktree file. `Sessions.spawn_stage` takes an optional
keyword `ticket=''`, passed explicitly by main._launch_stage for implementation slices.
Every real launch requires state_dir. The main dispatcher passes the implementation cursor
as the ticket string before saving updated task state; resume preserves a matching previous
binding's ticket, otherwise it uses the known continued task stage/cursor. Native session
records use the binding's stage even while the saved task still names its previous stage.
Dry runs create no runtime snapshot.

`RuntimeClient` uses the existing listener's Unix HTTP socket for mutations. Reads
can use the immutable atomic snapshot. `waitd.handle_ping` accepts launch-bound Claude
hook events; unqualified legacy pings cannot modify a prepared launch. Stale bindings,
foreign threads, old turns and retired launches are rejected. Starting input clears
waiting evidence while preserving the wait clock.

Conditional retirement returns retired/held/unknown. It compares revision and binding
and atomically fences future input; only retired permits an automatic stopped-turn or
background-cap park. Accepted input and pending delivery hold retirement. Stage signals
and crash handling retain existing authority. A cap only applies to a stopped turn with
known running owned work, and strictly after 10800 seconds by default. A stopped report
containing new work resets the clock; foreground input, outcomes, delivery and reconnect do not.

### Input admission and retirement wire contract

`RuntimeClient.accept_input(binding, client_message_id) -> bool` and
`RuntimeClient.retire(binding, revision, *, reason='stopped', now=None, cap=10800) -> str`
use the same sole-writer listener. `BoundClient.accept_input(binding, client_message_id)`
has no host credential and requires its owning target, issue and launch to match the
explicit binding. `/runtime/input` accepts `{binding,client_message_id}` and returns a
JSON boolean. Host-authenticated `/runtime/retire` accepts
`{binding,revision,reason,now,cap}` and returns a JSON string.

Successful admission is durable before forwarding upstream and invalidates older stopped
evidence even when a new turn-start notification has not arrived. Independent admitted
inputs retain separate receipts; acknowledging or rejecting one cannot release another.
Per-connection JSON-RPC request IDs are not durable input identity. Uncertain receipts
survive listener restart and remain held, without blind retransmission. A repeated native
prompt callback cannot establish another input or main turn.

Retirement reasons are `stopped`, `background` (cap), and `forced` (explicit physical
closure). Automatic retirement requires live, known stopped main state, known inventory,
no pending input/results, and current binding/revision; unknown evidence holds. Forced
closure preserves explicit stage/cancellation authority despite running work, but still
requires the exact current launch. A successful fence rejects all later terminal,
controller-bootstrap, result and native main input before physical closure.

The installed Claude UserPromptSubmit command identifies its native event kind separately
from stdin and exits 2 when admission is rejected or uncertain, including malformed or
mismatching main payloads. Accepted main input exits 0. Supplementary child hooks do not
gain main lifecycle authority, and unmanaged legacy hooks retain their existing behavior.
The acceptance seam executes the command installed by `workspace.install_stop_hook`,
rather than prescribing its internal argument spelling.

## Controlled Codex session

A container supervisor starts app-server, the runtime controller, a local Unix WebSocket
gateway and the real `codex --remote` terminal for the exact bound root. The gateway
serializes operator turn RPCs, result RPCs and host retirement fences. It records durable
sent-unconfirmed receipt state before forwarding. The source package and installed Python
WebSocket dependency are mounted read-only. No host runtime snapshot is mounted.
Service death exits the session; controller disconnect reconnects without replacing it.

The supervisor's executable seam is `python3 -P -m dispatcher.codex_supervisor
--model MODEL --prompt-file PATH [--effort EFFORT] [--resume CONVERSATION_ID]`.
It runs in the task worktree inside the container. Python safe-path mode and an
explicit read-only source path prevent the target worktree's own Python packages
from shadowing the deployed supervisor. The immutable target, issue and launch ID
select the complete prepared binding through the listener's exact-launch view API;
the host credential remains outside the mounts. The prompt is read from its file,
not expanded into the host or container argument list. Fresh launches create one
conversation; resumes select exactly the supplied ID, including IDs resembling flags.
Binding precedes the actual stage prompt and real remote terminal attachment, without
a synthetic model turn. `codex` on PATH and its Unix WebSocket app-server are the
external process/transport boundaries for isolated fixtures.

The controller reads authoritative lifecycle, complete background-terminal inventory,
thread history and descendant ancestry. Newly observed commands become eligible background
workers only when a complete authoritative inventory confirms they survive the matching
normal main Stop. Codex 0.156.1 lists foreground processes in backgroundTerminals/list too;
its tested command/hook stream exposes no supported initial-yield receipt. Commands ending
before that qualification must not be redelivered as independent background results.
Eligibility persists for confirmed workers during subsequent active main turns. It batches
already available eligible results promptly.
It steers an active exact turn or starts an idle continuation in the same conversation.
A confirmed turn mismatch retries after a fresh authoritative read; a lost acknowledgment
must reconcile a matching client message ID in history before further transmission.
Unknown acceptance holds the result and alerts once; acknowledgment/history resolves it.
Native child-result notifications are supplementary and never create extra batches.

### Owned inventory and background clock

[Runtime contract](runtime-contract.md) declares the T5 normalized public worker
identities, ancestry, inventory events, available outcomes, launch history checkpoint
and wait clock. Commands retain their initiating owner/initial item through later
interactions. Qualification requires a complete inventory observation begun after
the owning normal Stop; a delayed response does not change that causal ordering.
Child commands use exact owning-child completion evidence, which cannot end the
main turn. Descendant discovery validates every link and complete scoped pages;
unreadable inventory preserves known work and remains unknown.

Known outcomes cannot be overwritten by stale running observations. Fresh physical
resume seeds prior work before its prompt; same-launch reconnect retains that
checkpoint and current results. Only new eligible running work reported while the
main turn is stopped resets the clock. The durable ever-reported union prevents an
old worker's reappearance from resetting it. Exact cap equality holds; automatic
background retirement requires stopped, live, known work and elapsed time strictly
greater than the configured cap. Pending input/results continue to hold retirement.

## Compatibility and verification

Baselines: Codex 0.156.1 and Claude 2.1.288. Other versions require passing capability
checks; unsupported payloads hold and alert. Tests use isolated homes and a scripted fake
provider. Actual child-agent behavior, native Claude hook payloads, real TUI attachment and
the herdr/Podman launch must be verified before integration is marked complete.

## Data-model attack ledger

1. Input accepted between dispatcher read and park: revision/fence must prevent killing work.
2. Acceptance persisted by Codex but acknowledgment lost: resetting to unsent duplicates turns.
3. Previous ticket callback reaches a resumed same conversation: fresh launch identity must reject it.
4. Empty terminal inventory while a nested child runs: ancestry and inventory certainty must hold capacity.
5. Partial completion or reconnect resets clock: cap can be evaded indefinitely.

Identity inventory: waiting/background/session record files are covered by launch snapshots
for managed launches; legacy markers remain migration inputs only. Worker/completion/delivery
keys, runtime socket paths and seen turns are launch-scoped. Task-state filenames, task
branches/artifacts, herdr labels, operator queues, intents and event target/issue fields retain
existing task identities; they are outside the runtime identity change.
