# T6 runtime delivery contract

Approved after verified T5 merge `6f781de` and documentation checkpoint `2ca7526`. T6 acceptance and implementation follow the isolated locked-test workflow.

## Scope and retained authority

T6 promptly transmits available T5 command/agent outcomes and confirms successful native
ACKs. Durable sent-unconfirmed state is a T6 safety prerequisite. History reconciliation,
lost-ACK recovery, and general controller/dispatcher restart behavior are T7. Alert
presentation, resolution notifications, and actual herdr/Podman verification are T8.
Those later boundaries do not permit T6 to retry uncertain input, erase durable uncertainty,
invent history receipts, or discard existing alerts.

Retain the version-1 snapshot, complete prepared binding, exact-launch routes, `main`,
`inputs`, per-turn completed revisions/provenance, retirement fence, T5 `workers` and
`completions` lists, `history_checkpoint`, `wait`, and `alerts` at their existing locations.
T5 WorkerIdentity, CompletionRecord, outcome, ancestry, qualification, history-baseline,
and clock declarations remain unchanged. Lookup uses identity, not list order.
The host listener/RuntimeControl remains the sole runtime writer; the dispatcher remains
the sole TaskState writer. Neither the gateway nor controller writes snapshot files or
task state. No new public mutation callable is introduced.

Retained T4 public shapes:

```text
Main = {status: 'unknown' | 'active' | 'stopped', turn_id: string | null,
        seen_turns: list[string], completed_turns: map[turn_id, positive revision]}
InputReceipt = {status: 'pending' | 'accepted' | 'settled' | 'rejected',
                turn_id: string | null, revision: positive integer}
Snapshot.inputs = map[client_message_id, InputReceipt]
{type: 'input/accepted', client_message_id: string, turn_id: string}
{type: 'input/rejected', client_message_id: string}
```

Input admission reserves a previously absent identity as pending/null at its durable
admission revision; reused IDs are rejected. Accepted/settled receipts retain their exact
nonempty turn ID; rejected/pending retain null. Every input settles independently only
when its exact accepted turn's completed_turns revision is strictly after its admission.
All recorded input/end revisions are at most the enclosing snapshot revision. The current
known main turn is the last unique seen_turns entry; unknown main retains history with
null current turn. Delivery acceptance must not change those lifecycle invariants.

All delivery state belongs to one complete launch binding: target, issue, stage, ticket,
launch_id, runtime, worktree, and exact recorded conversation_id. A physical resume has
a fresh launch. Old-ticket/old-launch/foreign events cannot assign, send, acknowledge, or
revive current results. Native child Stop/result notifications are supplementary; they
provide neither main lifecycle authority nor an independent controller delivery.

## Observable records

Keep `view(...).deliveries` as a list of logical batch records, embedding
their physical attempts. This avoids another top-level collection. The completed T5
records remain in `completions`; batch membership records assignment without deleting
outcomes or extending/changing CompletionRecord. Strings below are nonempty opaque IDs.

```text
ResultInput = list[{type: 'text', text: string}]       # nonempty list

DeliveryBatch = {
  batch_id: string,
  completion_ids: list[WorkerIdentity],              # nonempty, no duplicates
  input: ResultInput,                               # exact immutable native input
  created_revision: nonnegative integer,
  status: 'pending' | 'confirmed',
  attempts: list[DeliveryAttempt]
}

DeliveryAttempt = {
  attempt_id: string,
  client_message_id: string,
  method: 'turn/steer' | 'turn/start',
  thread_id: string,                                # exact bound root
  expected_turn_id: string | null,                  # required for steer, null for start
  admission_revision: positive integer,
  status: 'sent-unconfirmed' | 'confirmed' | 'rejected',
  receipt: DeliveryReceipt | null,
  rejection: {kind: 'expected-active-turn', code: -32600,
              message: string} | null
}

DeliveryReceipt = {
  source: 'ack' | 'history',
  thread_id: string,
  turn_id: string,
  item_id: string | null                            # null for ACK, native ID for history
}
```

All membership, input, IDs, method/precondition, and admission provenance are immutable
once recorded; only declared status/receipt/rejection transitions change. A batch retains
its same identity, exact input, and completion membership after a proven rejection.
A later safe physical attempt gets a fresh attempt_id and client_message_id; its method
and exact-turn precondition reflect a fresh authoritative read. JSON-RPC envelope IDs
are transient per-connection correlation, never batch/attempt/client-message identity.
The durable client ID is sent as native `clientUserMessageId`; native history exposes it
as `userMessage.clientId`. Generate IDs with no reuse across operator/bootstrap/result
inputs or launches. No ID encoding/UUID algorithm or storage layout is chosen here.

`confirmed` means upstream accepted the input, not that the agent processed it, the worker
succeeded, the main turn ended, or the task/stage completed. A batch confirms only when
its own attempt confirms. One receipt never resolves another batch or operator input.
An arbitrary error, timeout, disconnect, malformed response, or failed durable receipt
write leaves the physical attempt sent-unconfirmed; it is not a declared rejection.
T7 may extend explicit proven-rejection kinds only with approved public evidence.

Every batch includes all currently unassigned available T5 completions at its accepted
proposal revision. It need not wait for running workers, another result, or a dispatcher
pass. Later completions go into a subsequent immutable batch. Assignment is one-to-one
within the launch, retained across duplicates and reconnect; an existing assigned,
confirmed, or sent-unconfirmed completion cannot enter another batch. An idempotent replay
of the identical batch/attempt event changes nothing; same ID with different content is
rejected. Do not append late outcomes to already-recorded input.

Each ResultInput contains exactly one text item whose entire text is standalone JSON
decoding to a nonempty list[CompletionRecord], with each existing public record precisely
{identity,outcome}. Additional prose text items are permitted. Decode full typed records
and compare by complete worker identity; JSON list/key order, whitespace/escaping,
prose wording, generated ID encoding and serialization algorithm are unspecified.
The host listener validates that the proposed semantic records exactly match the immutable
completion membership and available stored outcomes before assigning any completion.
Wrong, missing, extra or duplicate records, malformed payloads or altered native values
reject the proposal atomically. Within a recorded batch, its entire native input remains
exactly immutable and captured upstream input must equal that recorded input. Preserve failed/declined command status, available exit code/output,
duration, child messages and errors; null remains unavailable, distinct from empty/zero.
Do not concatenate overlapping native output deltas/aggregates, invent missing lifetime
prefixes, or turn child error/empty history into success. Failure alone does not park.

Successful native history queries can expose different snapshots of the same exact owning
turn. Preserve every safe available exact-turn message exposed before publishing its
completion, including a message present in exhausted full-turn pages but absent from an
earlier successful item-list response. Query success or query order does not justify
discarding that available message. Preserve original item/turn identity and terminal
status; no new completion or invented message may result. General later-history recovery
after an already-published completion remains T7.

## Operations through `event`

Use the existing public methods and sole listener:

```python
RuntimeControl.event(binding, event, *, now=None) -> bool
RuntimeClient.event(binding, event, *, now=None) -> bool
BoundClient.event(binding, event) -> bool
```

Existing wire: `POST /runtime/event` with `{binding,event,now?}`, boolean JSON response.
Exact-current binding and existing host/container authorization apply. Events:

```text
{type: 'delivery/proposed', observed_revision: integer,
 batch_id: string, completion_ids: list[WorkerIdentity], input: ResultInput}

{type: 'delivery/sent', observed_revision: integer, batch_id: string,
 attempt_id: string, client_message_id: string,
 method: 'turn/steer' | 'turn/start', thread_id: string,
 expected_turn_id: string | null}

{type: 'delivery/ack', batch_id: string, attempt_id: string,
 client_message_id: string, thread_id: string, turn_id: string}

{type: 'delivery/rejected', batch_id: string, attempt_id: string,
 client_message_id: string, rejection: {kind: 'expected-active-turn',
 code: -32600, message: string}}
```

Proposal is an atomic compare against the current observed revision, freshly read task gate,
and complete set of unassigned outcomes. A stale proposal returns false, with no partial
assignment; re-read and propose the current set. An accepted proposal records immutable
membership/input durably. Unsent queued batches do not authorize upstream transmission.

`delivery/sent` is the atomic admission-and-persistence boundary: validate current launch,
fresh task gate, main selection/precondition, method-specific input barriers, revision, and immutable
batch; record the physical attempt as sent-unconfirmed and its corresponding existing
T4 input-admission/receipt hold before returning true. The gateway forwards only after
that true response. False/unknown response permits no send. Preserve T4 individual input
provenance and completed-turn revisions; do not maintain a competing result input ledger
that could clear another admitted input. Record the actual listener revision as
admission_revision. This declaration chooses observable atomic behavior, not storage.

The same snapshot atomically creates the existing `inputs[client_message_id]` pending
receipt with null turn_id and that same positive admission revision. Do not call ordinary
accept_input first and then separately persist the attempt: either both reservations
are durable or neither is admitted. Preserve ordinary accept_input semantics for all
other callers, including identity reuse rejection and stopped-evidence invalidation.

ACK/rejection events resolve only the exact persisted pending attempt/client identity.
For steer, ACK turn_id must equal expected_turn_id. For start, use the valid native
returned Turn's ID. The gateway validates the correlated native response before reporting
the normalized event. Success applies the existing `input/accepted` semantics and updates
the matching T4 receipt and batch atomically. It yields accepted, or settled when the
exact completed_turns revision is already strictly greater than admission_revision.
Known rejection applies existing `input/rejected` semantics to only its own receipt.
An invalid or foreign receipt returns false and cannot free pending work. A repeated
identical receipt is idempotent. A contradictory late rejection cannot undo confirmation.
Do not require an ACK to synthesize or overwrite current main lifecycle.

T7 should approve a separate history-receipt event with exact root, attempt client ID,
native item ID/turn ID and matching immutable input; `DeliveryReceipt.source='history'`
reserves that representation. No T6 operation pretends a history read happened.

## Task, stage, signal and ticket gate

At every proposal and send admission, the host listener freshly reads existing public
`load(state_dir,target,issue)` and `read_stage_signal(worktree)` evidence. Require the
exact target/issue, recorded worktree, continued
stage and implementation ticket/cursor to match the current launch, an unparked live
working-stage task, and a matching `working` StageSignal. Snapshot service/launch state
must also permit delivery. Missing, malformed, mismatched, unreadable, nonworking, or
conflicting evidence disables proposal/send without changing task state, stage transitions,
operator-terminal access, or existing physical-closure behavior. No cached enabled flag,
new gate event, snapshot gate collection, or controller assertion supplies this authority.

A fresh prepare may precede the dispatcher's TaskState write. Hold result proposal until
matching task state is visible; preserve ordinary bootstrap/operator admission semantics.
Read the gate again before send even when a previously accepted proposal exists. Do not
copy configuration loop-limit policy into this decision: the existing dispatcher retains
loop-limit and closure authority, and its exact-launch forced fence blocks result admission
once parked. Results cannot override a changed task/signal or that fence.

Existing `blocked`, `awaiting-answers`, `awaiting-ci`, `awaiting-review`, `done`, retry/loop
limits, cancellation, replacement and crash transitions win. A matching `working` signal
can corroborate ongoing work but cannot override dispatcher state or a retired launch.
A signal for another stage or TaskState for another ticket is not current approval. StageSignal has no ticket field; TaskState.ticket_cursor supplies the IMPLEMENT ticket. Do not repurpose its CI run_id. REVIEW `done` may move to
PR_OPEN without physically ending its session: no late result may continue that completed
REVIEW binding even while its terminal/service remain alive. Do not add session closure
to that existing transition. A child notification or StageSignal `done` is never main
normal Stop or result-receipt authority.

## Gateway ordering and native delivery

Operator/bootstrap/result inputs share the existing gateway input lock and exact-launch
listener fence. Acquire that lock before selecting/revalidating main state and performing
result admission. Accepted operator input and an idle result proposal have one order:

- If operator input wins, its durable admission invalidates old stopped evidence. A result
  cannot start a competing idle continuation while that input could own/create a turn.
  Select a fresh authoritatively active exact-root turn and steer it when known; otherwise
  hold. The operator's unresolved receipt remains separate even after a successful read.
- If the result wins while current root is authoritatively idle and all input barriers are
  settled/rejected, admit/persist/send one same-root `turn/start`. Later operator input follows existing
  ordering and targets the resulting active turn when appropriate.

Active delivery is `turn/steer` with exact bound `threadId`, exact current
`expectedTurnId`, fresh attempt `clientUserMessageId`, and the immutable batch `input`.
Idle delivery uses `turn/start` with that same exact thread, client ID and input. No
thread/start, latest-thread selector, interruption, replacement terminal, or new root is
permitted. Unknown/systemError/notLoaded/inconsistent active state is not idle authority.
History's completed turn alone does not fabricate current normal Stop.

Each batch retains its own stable physical attempt and independent receipt. An existing
sent-unconfirmed batch never resends. Newly available unassigned outcomes can form another
batch promptly, and the shared gateway lock may admit/send that distinct batch as an exact
active-turn steer even while other operator/bootstrap/result receipts remain unresolved.
An authoritative active same-root read and expectedTurnId guard that send; the read does
not confirm, reject, settle, or clear any other receipt. Always revalidate under the lock.

The shared input lock orders selection, durable admission and physical forwarding. It
must not remain held while awaiting an upstream ACK: acknowledgment/uncertainty handling
runs independently so an operator or a distinct eligible active batch can progress while
an earlier reply is delayed or lost. Inventory polling must likewise continue without
waiting for that earlier receipt. This clarifies the existing independent-batch rule;
it supplies no permission to resend the earlier attempt.

Idle `turn/start` requires no unresolved pending/accepted input that could already own or
create another turn. Uncertainty therefore holds idle continuation, without imposing a
global prohibition on safe distinct active steers. An active steer can finish before its
ACK arrives; its normal Stop neither confirms that attempt nor releases its retirement
hold. If a distinct batch has a newly authoritative active target, it can steer under the
rules above; if the root is idle with unresolved input, it holds. Persisted sent-unconfirmed
survives even if forwarding did not occur after persistence; do not guess that it was unsent.

Only a correlated persisted exact-root turn/steer attempt with a recorded nonempty expected turn
and the exercised -32600 expected-ID mismatch message, or exact -32600 message
`no active turn to steer`, establishes nonacceptance for that attempt. Retain immutable batch membership/input, settle only its
own rejected input, perform a fresh authoritative exact-root read and gate revalidation,
then a new safe physical attempt may select active steer or idle start. A generic or altered -32600 message,
another arbitrary RPC failure, uncertain ACK, or history's temporary absence is not this
proof and permits no blind resend or speculative transmission. Match native evidence and
the recorded expectedTurnId; no provider ID-string parse algorithm is specified. Unsupported
methods/errors remain pending. The older T4 fake -32602 example is fixture behavior and
does not define an additional T6 safe-retry rejection class. T7 history may later reconcile.

## Causal normal Stop and retirement

A successful result admission retains T4 input provenance and invalidates earlier
stopped evidence before forwarding. A later authoritative normal main completion for
the accepted exact turn, recorded after that attempt's admission revision, supplies
the causal Stop relationship; it may arrive before the matching ACK. Keep the result
hold until that ACK confirms. Once confirmed, use that later recorded Stop through
existing per-turn rules rather than demand another Stop solely because the ACK was late.
An earlier Stop, another turn's Stop, supplementary native Stop, child completion, or
historical completed status cannot discharge the admission hold. A late ACK must neither
revive an obsolete turn nor erase an unrelated input's uncertainty.
In particular, delayed ACK for accepted turn A during current active turn B can settle
only A's receipt through `main.completed_turns[A] > receipt.revision`; B remains active.
Neither delivery confirmation nor receipt settlement pretends any main turn completed.

Pending available outcomes, queued batches, and sent-unconfirmed attempts hold ordinary
input parking and automatic background-cap retirement. Confirmed delivery removes only
its own result hold; ordinary parking still requires current authoritative stopped main,
known complete empty work, settled inputs, eligible task state, and matching revision/fence.
Delivery does not reset wait.since/ever_reported. Existing cap equality, strict greater-than,
foreground exemption, capacity ownership, and fresh-resume rules remain unchanged.
Existing explicit closure still uses exact-launch forced retirement before any physical
end it performs. Service death follows Failed/Resume rules; result delivery cannot restart
the service. All private-store isolation, read-only wait/source mounts, immutable launch
selectors and container path validation remain required.

## Native 0.156.1 evidence and limits

Public generated schemas declare required steer threadId/expectedTurnId/input, optional
clientUserMessageId, required ACK turnId; start requires threadId/input and returns Turn.
Native receipt probes establish a valid active steer ACK for its exact turn, and completed
exact-root history contains the corresponding `userMessage.clientId` and input. A dropped
owned ACK was recovered through exact-root resume/read/exhausted items history with one
submission and no extra turn. The stale expectedTurnId returned -32600, retained the active
root, and had no matching input after completion. Two repeated identical client IDs/input
were both accepted and persisted as distinct native user-message items. Client IDs are
correlation, not native deduplication.

History scans were exact-root, opaque cursor pages to final null, corroborated by full
read. Active-turn receipt persistence timing, arbitrary history absence/retention, backend
restart/crash, other versions, and generic rejection classes were not established. The
lost-ACK probe dropped an already-arrived owned reply; it does not mean all timeouts are
acceptance. Exact-root resume subscribes; read alone does not. T7 retains those boundaries.

Source references (portable identifiers, no installed temporary-path dependency): approved
`proposal.md`, `spec.md` requirements 1–15, `design.md` RuntimeControl/input/gateway sections,
`tasks.md` T5–T8, and `runtime-contract.md` T5; public T5 `provider-contracts.md`; public
T4 `public-declarations.md`, `additional-public-declarations.md`,
`wire-and-fixture-contracts.md`; native receipt `findings.md`, `summary-public.json`,
`concise-proof.json`; retained T4 `retained-input-contract.md`; exact-version schemas `v2/TurnSteerParams.json`,
`TurnSteerResponse.json`, `TurnStartParams.json`, `TurnStartResponse.json`,
`ThreadResumeParams.json`, `ThreadReadParams.json`, `ThreadItemsListParams.json`,
`ThreadItemsListResponse.json`.

## Acceptance criterion inventory

Behavioral coverage uses the existing listener, supervisor executable, external fake
codex app-server/remote terminal, Sessions and dispatcher seams:

| Criterion | Public observable assertion |
| --- | --- |
| Prompt batching | A reaches the main while B runs; proposal includes all unassigned available outcomes at its accepted revision, with immutable membership/input. No dispatcher-pass wait. |
| Outcome fidelity | Full typed worker identity and semantic command/child outcome values survive; null/empty/zero remain distinct; no wording/order/ID-encoding assertion. Failure alone does not park. |
| Independent successful history views | A safe exact-turn message exposed by complete full-turn pages before first completion publication survives an earlier successful empty item-page response and reaches the main exactly once with its owning outcome. |
| Root/turn selection | Idle uses same-root turn/start; active uses exact expectedTurnId steer, without interruption, extra root, foreign results or foreground-output redelivery. |
| Duplicate isolation | Repeated outcome/native child notification creates no second assignment; identical event replay is idempotent; conflicting immutable data is rejected. |
| Durable send boundary | At upstream forwarding, snapshot already has sent-unconfirmed attempt plus its existing pending T4 input receipt at the same admission revision. Persistence failure/uncertainty prevents send. |
| Independent active batches | While D remains sent-unconfirmed, a newly available distinct batch E can steer a fresh authoritative active same-root exact turn; D remains pending with no resend and no cleared hold. |
| Operator/idle race | Operator admission prevents competing result continuation; fresh known active root permits a distinct steer even with unresolved operator receipt. Unknown/idle with pending or accepted turn-owning input holds turn/start. A read alone settles no receipt. |
| Proven turn mismatch | Only exercised correlated -32600 expected-ID mismatch or exact no-active-turn evidence rejects that attempt; fresh read/gate choose a new physical attempt/client ID for the same immutable batch. Arbitrary errors/unsupported methods remain pending. |
| ACK and Stop causality | ACK confirms only its attempt/batch/input; invalid ACK remains pending. Stop before ACK cannot confirm it. Delayed ACK A during active B settles A only through its strictly later recorded end revision; B remains active. |
| Fresh task gate | Missing/malformed/mismatched/nonworking task/signal holds proposal/send. Initial prepare holds until matching TaskState appears. Changed ticket, park, forced fence, or REVIEW done→PR_OPEN disables result send without new closure behavior. |
| Existing lifecycle | Pending outcomes/deliveries/inputs hold automatic park/cap; clock/checkpoint/outcomes remain. Existing help/CI/review/limits/crash paths and forced fences win; later eligible empty normal Stop parks normally. |
| Later slices | T7 covers lost-ACK/history/general restart; T8 covers alert presentation/resolution and actual Podman/herdr/isolation/attachment. T6 already preserves uncertain durable attempts without resend. |

The accompanying public declarations preserve existing T4 input records and supply
exact task/signal gates, executable flags and exercised native rejection facts.
