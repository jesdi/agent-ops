# T5 public runtime contract

Approved public declarations for T5 acceptance, based on the approved Stage 2 plan
and native 0.156.1 evidence. These extensions are not yet implemented. The listener remains the sole runtime writer; the dispatcher remains the sole
task-state writer.

This contract covers Codex-owned work, its eligibility, available outcomes, and the
background-wait clock. Controller result transmission and delivery receipts remain
T6/T7. Claude's approved native `background_tasks` contract remains available.

## Existing public operations

```python
RuntimeControl(state_dir)
prepare(target, issue, stage, *, ticket='', runtime='codex',
        conversation_id=None, worktree='') -> dict
view(target, issue, launch_id=None) -> dict | None
event(binding, event, *, now=None) -> bool
accept_input(binding, client_message_id) -> bool
retire(binding, revision, *, reason='stopped', now=None, cap=10800) -> str
```

The existing host/container Unix HTTP routes and exact binding requirements apply.
No new mutation callable is proposed. Inventory uses `event` through the sole
listener. `retire` returns exactly `retired`, `held`, or `unknown`; only `retired`
permits an automatic park followed by physical end.

The existing version-1 snapshot fields retain their locations. For this extension,
`view(...).workers` is a list of the declared stored command/agent workers and
`view(...).completions` is a list of the declared completion records. Identity
lookup, rather than list order, is observable. A stored completed worker need not
be removed merely because its running status ended. A checkpoint, when established,
is exposed at `view(...).history_checkpoint`; a retained snapshot without that
field has no established checkpoint, and a fresh launch may expose null until
seeding. The existing `main`, `inputs`, `wait`, `alerts` and binding fields retain
their approved T4 contracts.

## Identity declarations

All identities are scoped by the complete prepared launch binding. Public provider
IDs are nonempty strings; their contents have no additional controller meaning.

```text
TurnIdentity = {thread_id: string, turn_id: string}

CommandIdentity = {
  kind: 'command',
  thread_id: string,
  initial_item_id: string
}

AgentIdentity = {
  kind: 'agent',
  thread_id: string,
  turn_id: string
}

WorkerIdentity = CommandIdentity | AgentIdentity
NativeClaudeIdentity = nonempty string     # native background_tasks[].id
EverReportedIdentity = WorkerIdentity | NativeClaudeIdentity
```

A command's `thread_id` is its owning thread and `initial_item_id` is its initial
command item. Its initiating `turn_id` is permanent provenance, although that turn
is not an additional component of CommandIdentity. Process IDs, command text, cwd,
later interaction turns, and interaction item IDs are not replacement identities.
A child worker is one child-thread/turn pair; another turn of that thread is a
different worker. Source paths and native hook session IDs do not establish ownership.

## Normalized ownership declarations

```text
OwnershipNode = {
  thread_id: string,
  parent_thread_id: string | null,
  source_kind: 'bound-root' | 'thread-spawn' | 'other',
  source_parent_thread_id: string | null,
  depth: integer | null
}

Ancestry = list[OwnershipNode]
```

`bound-root` describes the exact recorded conversation, not a required native
SessionSource value. The captured root source is `vscode`; a root must not be
rejected merely because its source is not `appServer`.

For an owned descendant, Ancestry includes the descendant, every immediate parent,
and the exact bound root. Each descendant link has `source_kind='thread-spawn'`,
agrees between its two parent fields, and reaches the next recorded parent. Native
depth corroborates those links. A matching root-looking path, cwd, source label, or
ancestor-filter membership alone is insufficient. Other subagent sources and
independent/internal threads are excluded. An incomplete/unreadable intermediate
link leaves ownership unknown; it does not establish no work. An idle, completed,
or notLoaded parent does not sever an established descendant's ownership.

## Inventory event declaration

```text
InventoryEvent = {
  type: 'inventory',
  certainty: 'known' | 'unknown',
  workers: list[CommandObservation | AgentObservation],
  observed_revision?: nonnegative integer,
  history_checkpoint?: HistoryCheckpoint,
  message?: nonempty string
}
```

The approved empty setup `{type:'inventory',certainty:'known',workers:[]}` remains
valid. Optional new fields do not make that existing public setup unsupported.
An omitted checkpoint retains the launch's recorded checkpoint; it does not clear it.

`observed_revision` identifies the launch snapshot revision captured before the
relevant provider inventory requests were made. It describes the request's causal
position, not the time its response arrived. New root-command qualification requires
this field; the existing empty inventory setup needs no newly required field and
cannot qualify a command. Omitting it cannot establish new root-command post-Stop
survival. Previously established eligibility remains recorded.

The exact root normal-completion revision must be less than or equal to that
observation revision. A terminal reply to a request begun before the matching main
Stop does not qualify a foreground root command merely because its reply arrives
after Stop.

A descendant command instead requires exact normal owning-child-end evidence,
from its native child event or authoritative child history, observed before its
complete exact-owner terminal report. No child host snapshot revision or child-end
registration callable is required or declared. An optional launch observation
revision cannot substitute for that child evidence. Root Stop does not supply a
child's owning-turn completion. Unknown or mismatched causal evidence is insufficient
for new eligibility and cannot establish empty work.

`known` means a complete, readable, correctly scoped view of relevant owned work and
its identities. Descendant discovery and every relevant exact-thread terminal/history
scope have their required readable final pages and ownership evidence. A root-only
empty terminal list cannot provide that claim. Missing cursors, nonfinal pages,
unreadable queries, unsupported payloads, or unknown owning turns do not provide it.
Provider cursor optionality does not make an incomplete scan complete.

`unknown` permits partial definite observations while making no empty-work claim.
Previously known workers, eligibility, available outcomes, checkpoint, and wait clock
remain recorded. A formerly running worker absent from a later inventory remains
unknown until an authoritative outcome establishes its end. Readable emptiness after
loading a thread is also insufficient to invent that outcome.

Unsupported required native information produces unknown control/inventory and a
compatibility problem on the existing public surfaces. It must not provide a normal
Stop, empty worker set, successful result, or delivery receipt. Foreign, replaced,
or retired bindings cannot change the current launch.

## Command observation and stored worker

```text
RootNormalOwningStop = {
  thread_id: string,                       # exact bound root
  turn_id: string,
  status: 'completed',
  revision: nonnegative integer           # accepted authoritative main Stop
}

DescendantNormalOwningStop = {
  thread_id: string,                       # exact validated owning descendant
  turn_id: string,
  status: 'completed',
  evidence_source: 'native-child-event' | 'authoritative-child-history'
}

NormalOwningStop = RootNormalOwningStop | DescendantNormalOwningStop

CommandObservation = {
  identity: CommandIdentity,
  turn_id: string,
  ancestry: Ancestry,
  process_id: string | null,
  command: string,
  cwd: string,
  source: 'agent' | 'userShell' | 'unifiedExecStartup'
          | 'unifiedExecInteraction' | null,
  status: 'running' | 'completed' | 'failed' | 'declined' | 'unknown',
  inventory_running: boolean,
  normal_stop: NormalOwningStop | null,
  outcome: CommandOutcome | null
}

StoredCommandWorker = CommandObservation & {
  eligible: boolean,
  qualifying_stop: NormalOwningStop | null
}
```

`inventory_running` denotes the command's presence in authoritative inventory for
its exact owning thread. An item/started notification by itself does not establish
post-Stop survival.
`RootNormalOwningStop.revision` is the launch snapshot revision recording accepted
normal main completion for the exact bound root/turn. It precedes or equals the
inventory observation revision; neither a later-arriving main Stop nor a different
turn can authorize an earlier root terminal observation.

`DescendantNormalOwningStop` records exact normal completed child-turn evidence
observed before the complete terminal report for that command's exact owning thread.
Its native event or authoritative history must establish the initiating child turn's
normal end. It carries no required host revision. This is worker evidence and cannot
establish main completion, a main session record, or a main waiting marker. No new
child event or mutation callable is declared here. `normal_stop` denotes authoritative matching normal owning-turn
completion, never a caller's waiting marker or a timing/yield assumption. Root
commands require the current bound main turn's normal Stop. Descendant-owned commands
require their exact initiating owned thread/turn's normal Stop; the additional
grandchild-command capture demonstrates this case.

`eligible` and `qualifying_stop` are observable stored results, not caller authority
to qualify arbitrary work. A newly observed command becomes eligible only when
authoritative inventory confirms that it survives that matching normal Stop.
Before it first qualifies, a terminal present while its owning turn is active remains
an observed command and does not establish independent background eligibility.
Failed/interrupted turns, child completion mistaken for main Stop, stale turns,
unreadable evidence, or a supplementary native Stop cannot qualify a root command.

Once established in this launch, eligibility persists across later active main turns
and same-launch reconnects. The command keeps its initial item and initiating turn
even when a later terminalInteraction names another turn. A command ending before
its first qualifying Stop does not create an independently available completion for
redelivery. Its terminal observation can retain available outcome data without
becoming a pending background result.

The declared `unifiedExecInteraction` source is not proof of initial-item linkage.
An observation without established initial command identity/ownership is unknown;
a new interaction item cannot become a new independently qualified worker merely
because that source enum exists.

## Agent observation and stored worker

```text
AgentObservation = {
  identity: AgentIdentity,
  ancestry: Ancestry,
  thread_status: 'active' | 'idle' | 'systemError' | 'notLoaded',
  active_flags: list['waitingOnApproval' | 'waitingOnUserInput'],
  status: 'running' | 'completed' | 'failed' | 'interrupted' | 'unknown',
  outcome: AgentOutcome | null
}

StoredAgentWorker = AgentObservation & {eligible: boolean}
```

A validated task-owned running child turn counts at every depth when the main turn
is stopped. The command-survival qualification rule does not require a child turn
to complete before that running child can count. Thread-list `turns:[]`, notLoaded,
missing native SubagentStop, or an empty terminal list does not establish child
completion. Exact child turn/error evidence can establish a failed child even when
its item history is empty. Active approval/input flags do not establish completed
work; existing explicit main-stage requests for help retain their authority.

A spawning `subAgentActivity(kind='started')` completing its own item does not
complete the child's turn. Native child hooks remain supplementary and cannot end
the main turn or create an additional controller result.

## Available outcome declarations

```text
CommandOutcome = {
  status: 'completed' | 'failed' | 'declined',
  exit_code: integer | null,
  aggregated_output: string | null,
  duration_ms: integer | null
}

AgentOutcome = {
  status: 'completed' | 'failed' | 'interrupted',
  messages: list[{item_id: string, text: string}],
  error: {message: string, codex_error_info?: public JSON value | null} | null
}

CompletionRecord = {
  identity: WorkerIdentity,
  outcome: CommandOutcome | AgentOutcome
}
```

An available completion belongs to an eligible current-launch worker. Its identity
is stable across duplicate native events, history records, and same-launch restart;
those observations do not create another completion. Known terminal outcomes are
durable in `view(...).completions`; controller transmission remains T6. An unresolved
available result keeps the ordinary input-park decision held under the T4 contract.
There is no T5 fixture operation that pretends a result was delivered.


A known terminal outcome remains durable when an older inventory observation later
reports that identity as running. Its status, exit code, and available output are
not overwritten by that stale reply, and the reply does not create a new worker or
completion. Unreadable later inventory also does not erase an established outcome.

The native failed initial item with `exitCode=-1` and `aggregatedOutput=null` yields
`{status:'failed',exit_code:-1,aggregated_output:null,...}`. Its subsequent notLoaded
status and thread-not-found query do not erase that failure or relabel it as success.
The cause of that sequence is not established. Failure alone does not park the task.

Null output means unavailable output, distinct from an observed empty string. No
missing exit code becomes zero. Aggregated output is the available native aggregate,
not a claim of full lifetime output: captured prefixes are absent and a PING delta
overlaps its aggregate. No concatenation or full-output reconstruction is declared.
Reduced notification items are not complete item history. A definitive failed turn's
error remains usable when the corresponding item-history result is empty.

## Launch history checkpoint declaration

```text
HistoryScopeCheckpoint = {
  thread_id: string,
  turn_id: string | null,
  cursor: string | null,
  complete: boolean
}

HistoryCheckpoint = {
  launch_id: string,
  seeded: boolean,
  baseline_turns: list[TurnIdentity],
  baseline_workers: list[WorkerIdentity],
  seen_completions: list[WorkerIdentity],
  scopes: list[HistoryScopeCheckpoint]
}
```

The checkpoint selects the same exact launch as the enclosing binding. A fresh
physical launch, including a resume of a recorded conversation, has a fresh history
baseline. `baseline_turns` and `baseline_workers` identify already-present native
work/history excluded from current-launch completion claims. Old outcomes and delayed
old events must not become newly available results for that replacement launch.
An unreadable seed is `seeded=false`, not an empty old-history guarantee.

After a successful seed, the baseline is stable for that physical launch. Same-launch
reconnect does not reseed current worker completions as prior work, erase eligibility,
clear recorded outcomes, or reset the clock. Completion identities already recorded
remain represented in `seen_completions`.

Scope cursors are opaque provider strings, scoped to the exact thread and optional
turn. Null with `complete=false` does not mean a completed scan; null with
`complete=true` records readable exhaustion for that observation. A persisted cursor
does not independently establish a worker's disappearance, prevent later history
changes, or promise recovery from every arbitrary checkpoint position. Running
startup items were absent from captured pre-completion history, so empty running-item
history cannot replace stored work identities or make inventory authoritative.

## Wait clock and retirement observables

```text
Wait = null | {
  since: number,                         # epoch seconds
  workers: list[nonempty string],         # retained native/T2 observable
  ever_reported: list[EverReportedIdentity]
}
```

`workers` retains the established native/T2 observable field. It describes the
currently reported running subset with nonempty string identities; native Claude
entries retain their exact `background_tasks[].id` values. Codex entries are stable
launch-scoped string labels for the declared typed WorkerIdentity; their contents
are not process IDs or a new ownership proof, and no string-encoding algorithm is
proposed here. The typed identity remains the Codex clock's identity authority.

`ever_reported` is the durable union of identities already reported as running
eligible owned work in a stopped main state during this physical launch. It stores
native Claude identities as their nonempty ID strings and Codex identities as the
stable typed command/agent values. The launch runtime distinguishes those identity
forms. Completion, omission, or a smaller current `workers` subset does not remove
identities from this union. Repeated reports or reappearance of an already-reported
identity cannot make it new again. A new native Claude ID, another initial command
item, or another turn of an owned child thread is distinct work.

A retained native/T2 Wait lacking the declared `ever_reported` field still retains
its recorded `since` and `workers`; absence of the extension is not permission to
reset its clock or discard already-reported native identities. These fields remain
declared for T5, with existing Wait observables preserved.

| Authoritative observation | Observable result |
| --- | --- |
| First stopped main report containing running eligible owned work | Wait starts at that report; task/session/capacity remain running |
| Later stopped report containing a genuinely new eligible identity | Wait resets to that report; new identity joins ever_reported |
| Same/subset work; old identity reappears | Wait and ever_reported clock origin remain unchanged |
| Foreground main input or a working main turn | Old stopped evidence is invalidated; wait clock remains |
| Partial completion, available outcome, future delivery, or same-launch reconnect | Wait clock remains |
| Unknown/incomplete inventory | No empty-work claim, ordinary input park, or automatic cap retirement follows from that uncertainty |
| Current main stopped, live service, complete known running work, eligible retirement, elapsed equal to cap | Hold; no cap park |
| Same state, elapsed strictly greater than cap | Confirmed background retirement permits existing cap park/session end |
| Current foreground main active, however old the wait clock | No background-cap park |
| Existing nonworking stage gate or explicit physical closure | Existing stage authority wins; exact-launch forced fence precedes any physical end it performs |
| Operator resume after cap park | Fresh physical launch and fresh wait clock |

Default cap is `background_wait_seconds=10800`. Accepted input and unresolved
available results continue to hold automatic retirement under T4; this contract does
not manufacture T6 delivery to bypass that hold. Unknown input or inventory is not
evidence for retirement. The existing background-cap reason/note remains observable.

## Native capability evidence and limits

The compatibility baseline is Codex CLI 0.156.1. Its generated experimental JSON
schemas declare the native request/response contracts, including ThreadListParams,
ThreadReadParams, ThreadItemsListParams, ThreadBackgroundTerminalsListParams and
corresponding responses. Schema declarations and exercised runtime behavior are
separate evidence. Native probes used isolated configuration and a local scripted
Responses provider with generic fixture prompts.

Runtime evidence exercises forward/reverse non-null cursor pages, depth-3 ancestry
with foreign-root exclusion, exact descendant terminal scope, initial command item
identity through later-turn stdin interaction, failed exit 7, and offline outcomes
recovered during a later active main turn. Exact-ID resume loaded an unloaded owned
parent without new input, turns, or root/TUI replacement; it does not guarantee that
every descendant worker survives loading. A failed initial item with exit -1/null
output preceded an unreadable grandchild; its cause was not established.

Remaining gaps are standalone unifiedExecInteraction initial-item linkage, running
startup-item history recovery, full lifetime output or initial-yield receipt, exact
reload of an unloaded owner while its previous command is still running, arbitrary
checkpoint positions or changing inventory across pages, declined-command runtime
outcomes, and runtime ancestry beyond depth 3. The approved any-depth rule is not
reduced to the deepest captured example. Missing required capabilities remain
unknown without invented outcomes, receipts, or empty inventory.

Separate native receipt probes also verified that successful `turn/steer` returns
the active `turnId`, and completed exact-root history retains its
`userMessage.clientId`. Dropping an owned acknowledgment and reconnecting recovered
that receipt without resending. An obsolete `expectedTurnId` was rejected with
`-32600` and no matching history input; repeating a client-message ID created two
distinct native input items in the exercised path. Client IDs provide receipt
correlation, not provider-side deduplication. Delivery recovery remains T6/T7.

Root approval clarification: a root normal Stop was authoritative while its owning
turn was current when the listener accepted it. A later main turn invalidates that
Stop as current input-park evidence, but does not erase its command-qualification
provenance. A newly observed command that demonstrably survives its own previously
accepted normal Stop can qualify during a later active main turn; the clock still
starts/resets only from a stopped-main report of new running eligible owned work.
No main Stop is fabricated from history.
