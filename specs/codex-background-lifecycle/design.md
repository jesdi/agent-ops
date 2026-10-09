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
is shared. Listener requests are serialized and snapshot replacement is atomic and
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

## Controlled Codex session

A container supervisor starts app-server, the runtime controller, a local Unix WebSocket
gateway and the real `codex --remote` terminal for the exact bound root. The gateway
serializes operator turn RPCs, result RPCs and host retirement fences. It records durable
sent-unconfirmed receipt state before forwarding. The source package and installed Python
WebSocket dependency are mounted read-only. No host runtime snapshot is mounted.
Service death exits the session; controller disconnect reconnects without replacing it.

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
