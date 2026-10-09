# T7 reconnect and receipt contract — public contract

Approved T7 public contract, following verified T6 merge `e77f9ee3a15bc76a1941874abdcf356866f5e7ff` and docs closure `bc3a975803597a6458d808d5cda06361d2dd1bde`. These declarations define required behavior for isolated acceptance and implementation; they do not claim T7 is implemented or complete. The isolated writer starts only after Root publishes this contract and explicitly activates its owned worktree.

## Retained boundary and authority

Use existing `RuntimeControl.prepare/view/event/accept_input/retire`, the exact-launch
Unix HTTP routes, BoundClient, supervisor executable, and Gateway seams. Add no mutation
callable. The host listener remains the sole snapshot writer; the dispatcher remains the
sole TaskState writer. Controllers, gateways, and terminals never write host snapshots.

Retain the complete prepared binding, version-1 snapshot, service/main/input fields, T4
input admission and accepted normal-completion provenance, T5 workers/completions/history
checkpoint/clock, and T6 batches/attempts/immutable native input. Identity lookup never
uses list order. Each physical launch, including an operator resume, is distinct; a
controller or dispatcher reinstantiation during that launch is not another launch.
Reinstantiation first reads the exact-current public view. Missing/malformed/unreadable
managed state is unknown and never permission to prepare, reseed, bootstrap, or resend.

All proposed events require the exact current complete binding, as refined after a
successful initial `bound`. Foreign, replaced, old-ticket, retired, or contradictory
events change nothing. Same-launch reconnect preserves durable input holds, bootstrap
state, worker identities/eligibility/outcomes, batch membership/attempts, checkpoint,
`wait.since`, `wait.ever_reported`, and alerts. A stale running observation cannot erase
a known outcome. Receipt recovery is observation, not new input admission or task revival.

## Minimal durable bootstrap declaration

A missing conversation ID cannot distinguish an unattempted fresh root from a lost
`thread/start` reply. Existing input receipts alone also cannot retain the exact initial
input needed for history matching. Declare one optional singleton `view(...).bootstrap`
record, not another collection of bootstrap deliveries. New T7-managed Codex launch
preparation durably initializes `bootstrap: null`; this explicit null is the unattempted
first-launch provenance. Its absence on retained state does not prove no prior bootstrap
and must not be filled in retroactively. Legacy absent state remains recovery-unknown.
`bootstrap/root-attempted` may replace explicit null once with the immutable record;
mode selection, a revision number, service status or record absence alone grants no
root/input forwarding permission. Other runtimes retain their existing preparation.
The null-to-record transition uses the same exact-current binding/revision validation
and atomic persistence before root forwarding; no separate protocol flag is needed.

BootstrapInput retains the ordinary initial stage prompt as a nonempty list of exact
text items. It shares T6's immutable text-item shape but has no result-batch JSON payload
requirement; a stage prompt is ordinary text. Text strings, including whitespace and an
empty string, retain their native ordinary-input semantics. The enclosing input list
must be nonempty. The result-delivery payload requirement remains specific to T6 batches.

```text
BootstrapInput = nonempty list[{type: 'text', text: string}]
Bootstrap = {
  root_operation_id: nonempty string,
  root_method: 'thread/start' | 'thread/resume',
  requested_conversation_id: nonempty string | null,
  root_attempt_revision: positive integer,
  root_status: 'attempted-unconfirmed' | 'bound',
  initial_input: {
    client_message_id: nonempty string,
    input: BootstrapInput                    # exact ordinary stage prompt
  }
}
```

For start, requested_conversation_id is null; for initial resume it is the exact
explicit selected conversation. Root IDs, operation ID, initial client ID and input
are immutable. Root status becomes bound only when the exact operation's authoritative
root identity is durably bound. Binding does not establish initial prompt acceptance.
Initial input state is derived from the existing `inputs[client_message_id]`: absent
means not yet admitted only when this established Bootstrap record supplies the prior
root/input-attempt provenance; pending means sent-unconfirmed; accepted/settled means
accepted; rejected means proven nonacceptance. No competing acceptance ledger is added.

```text
{type: 'bootstrap/root-attempted', observed_revision: integer,
 root_operation_id: string, root_method: 'thread/start' | 'thread/resume',
 requested_conversation_id: string | null,
 client_message_id: string, input: BootstrapInput}

{type: 'bound', conversation_id: string, root_operation_id?: string}

{type: 'bootstrap/sent', observed_revision: integer,
 root_operation_id: string, client_message_id: string, thread_id: string}
```

`bootstrap/root-attempted` atomically records the prior root attempt and immutable
initial input before forwarding initial thread/start or selected thread/resume. A failed
or uncertain listener response permits no forward. A recorded attempted-unconfirmed
fresh root never permits another thread/start, even if the first forwarding never
occurred. A correlated valid root reply or matching operation-owned native root identity
can apply `bound` with that exact operation ID. An uncorrelated thread/started, cwd,
source, latest thread, path, or unrelated history cannot discover its root. The original
supervisor/Gateway may retain and report a late correlated reply without reissuing the
operation; recovery after losing both that correlation and the root ID is a stated gap.

With this record present, `bound` must name its root_operation_id and agree with the
explicit resumed ID where applicable. Operation-bearing bound events require that recorded
attempt; explicit `bootstrap:null` does not satisfy correlation. The retained legacy
no-operation bound event can perform its existing first bind when no attempt record exists,
including explicit null and absent-extension snapshots. It cannot replace a selected ID or
bind twice. That compatibility route supplies no managed bootstrap or forwarding permission;
managed production records the attempt before binding. Once an attempt exists, every bound
event must match it. Only the host can update the binding. Read its refined binding before
subsequent mutations.

`bootstrap/sent` validates the known exact root, launch/revision, gateway ordering and
existing ordinary bootstrap/input admission rules, then atomically creates its existing
pending/null T4 InputReceipt before forwarding the initial `turn/start`. It admits that
client identity only once. No input hold and send-state write can be separated. ACK uses
existing `input/accepted` with the native returned exact turn ID; unknown ACK remains
pending. The exact client/input record enables the history operation below. T6's fresh
TaskState/StageSignal gates remain result-proposal/send gates; this extension preserves
existing ordinary bootstrap/operator semantics rather than delaying them until dispatcher
TaskState publication.

Replay never grants another send: under the shared Gateway lock, forward only for a
newly admitted operation/input whose pre-admission exact view established no prior
attempt. A durable prior attempt, ambiguous response, reconnect, or process-local flag
never grants a new forward. Identical event replay is a no-change operation; conflicting
IDs/content are rejected. A boolean event acknowledgment is not a reusable send token.
JSON-RPC IDs are connection-local correlation; native client IDs are durable correlation,
not provider deduplication. New root creation has no native client-message idempotency key.

## Optional ordinary Codex input provenance on the existing receipt

Declaration: extend ordinary admission with an optional keyword argument and
optional wire field, storing provenance in the existing input receipt rather than another
input ledger. Existing two-argument callers, omitted/null provenance, and Claude hook
admission retain their established T4 behavior.

```text
NativeInput = list[UserInput]                # exact-version public native schema
TextReceiptInput = nonempty list[{
  type: 'text', text: string, text_elements?: []
}]

NativeInputProvenance = {
  method: 'turn/start' | 'turn/steer',
  thread_id: nonempty string,
  expected_turn_id: nonempty string | null,
  input: NativeInput
}

InputReceipt += {native_input?: NativeInputProvenance}

RuntimeControl.accept_input(binding, client_message_id, *, native_input=None) -> bool
RuntimeClient.accept_input(binding, client_message_id, *, native_input=None) -> bool
BoundClient.accept_input(binding, client_message_id, *, native_input=None) -> bool

POST /runtime/input {binding, client_message_id, native_input?: NativeInputProvenance}
  -> boolean
```

For supplied Codex provenance, the host validates the exact current complete binding,
known bound root, method and native schema-valid input; thread_id must equal the binding's
conversation_id. Steer requires its exact nonempty expected_turn_id; start requires null.
Do not accept supplied foreign/malformed provenance or partially reserve its input. The
listener atomically stores the exact immutable provenance with the usual pending/null
InputReceipt at that admission's actual durable revision, retaining the existing identity
reuse rejection, service/launch fence, independent hold and stopped-evidence invalidation
rules. No additional caller-supplied revision, admission event, task gate, ledger, or host
file access is added. Provenance and receipt either both persist or neither is admitted;
later status changes keep the original provenance and admission revision intact.

The ordinary Gateway supplies this optional provenance from the final native request
under its existing shared input lock, before forwarding. Forward only after the admission
is durably acknowledged, with exactly that method/thread/precondition/input and the
admitted client_message_id as native clientUserMessageId. Failure/uncertain admission
permits no forward or blind replay; a reserved-but-not-forwarded input remains uncertain.
No client-ID encoding or new retry policy is chosen. Do not patch content into an already
admitted legacy receipt, infer it from later history, or reuse that client identity to
upgrade missing provenance. Bootstrap/result content remains in its existing declared
records; do not add a second admission for those atomic reservation paths.

The supported semantic history comparison in this declaration is text-only with absent
or empty text_elements. Schema-valid richer input may be stored exactly if supplied, but
its semantic persistence/recovery has not been exercised. The Gateway can omit optional
recovery provenance for such input; ordinary admission, forwarding and correlated ACK
behavior still apply. Richer input does not acquire a new access/admission block merely
because this narrow recovery comparison cannot confirm it. A lost ACK for unsupported
content remains pending and is not guessed accepted/rejected or resent. Distinguish an
absent provenance field from supplied `input:[]`; neither invents a user-message receipt.

## Exact history receipt declaration

Declare a single history event for existing input receipts, including bootstrap and
T6 result attempts whose immutable input is already durable. Ordinary operator history
recovery uses the optional immutable native_input above when supplied and supported.
Retained T4 receipt records without it still have no durable exact content; no invented
operator-input content or inferred provenance is permitted.

```text
ReceiptHistoryScan = {
  thread_id: string,
  turn_id: string | null,                   # null for whole-thread scan
  sort_direction: 'asc' | 'desc',
  from_cursor: null,                        # full relevant scope, no skipped prefix
  final_cursor: null,
  complete: true
}

InputHistoryReceipt = {
  thread_id: string,
  turn_id: string,
  item_id: string,
  revision: positive integer               # listener's durable confirmation revision
}

InputReceipt += {history_receipt?: InputHistoryReceipt}

{type: 'input/history', client_message_id: string,
 thread_id: string, turn_id: string, item_id: string,
 input: TextReceiptInput, scan: ReceiptHistoryScan}
```

The controller exhausts readable native `thread/items/list` pages for the exact bound
root, with either no turn filter or this exact target turn filter. Keep scope and direction
constant, pass each opaque nextCursor unchanged, and require explicit readable final null;
missing cursor information, repeated/nonprogressing cursors, unreadable/malformed pages,
scope switching, or unsupported items leave this operation uncertain. Provider optional
cursor fields do not authorize declaring exhaustion. No cursor parsing, latest selector,
cross-root search, or broad thread-list correlation is authorized. Receipt reconciliation
starts at null because arbitrary checkpoint positions are not established to cover the
pending receipt; it does not discard T5's durable launch baseline or outcome checkpoint.

Match a native `userMessage` with exact nonempty `clientId`, nonempty native `id`, exact
containing `turnId`, and content equal to the immutable submitted input. Preserve text,
element order, whitespace, and array boundaries; no substring/JSON-semantic/result-text
search substitutes for this match. The public [native-text-input-public-declarations.md](t7-public/native-text-input-public-declarations.md)
supplement exercises the native addition of `text_elements:[]` to submitted text without
that field. For supported text-only result/bootstrap/operator input, omitted text_elements
and an explicitly empty array are therefore the same native semantic input. Persist the
original submitted structure; normalize only that omission-versus-empty comparison.
Nonempty/unsupported spans, extra input elements, other types, or changed content cannot
match that text input. Do not trim/rewrite text, reorder elements or drop generic unknown
fields. Richer semantic forms remain recovery-unknown even when ordinary admission/ACK
works. Native clientId is optional/nullable in schema; missing/null is no receipt. For
steer, containing turn must equal the stored operator provenance's or result attempt's
expected_turn_id. If multiple approved durable input sources exist, they must agree under
this same narrow comparison; disagreement cannot be resolved by choosing one. Start
can learn its accepted turn from the matching message, including an older turn hidden by
a later active turn. Native thread/item/turn/client IDs are opaque and never parsed.

An accepted event atomically confirms only the existing admitted input and, when it is
the matching T6 attempt, its attempt/batch. Store InputHistoryReceipt and the existing
T6 DeliveryReceipt `{source:'history',thread_id,turn_id,item_id}` in their existing
records; the receipt-local proof is needed for bootstrap as well as later settlement.
No completion is reassigned, no new attempt is created, and admission_revision remains
unchanged. Apply T4 settlement through an already accepted exact normal-end revision
strictly after admission when available. One batch's receipt cannot clear another input,
attempt, result hold, or alert condition. A successful ACK already recorded remains the
same acceptance if later matching history corroborates it; history can attach its proof
without creating another delivery. Repeated exact native item/event/page observations
are idempotent. Conflicting receipt identities/content are rejected, preserving existing
confirmation and surfacing unknown/compatibility evidence.

Require an unambiguous exact match for the attempted client identity in this readable
scope. The native repeat-ID experiment persisted two different items for two submissions;
the controller must never send a repeated ID and must never count duplicate pages/items
as another delivery. Distinct native items with an allegedly unique attempted client ID
cannot prove controller exactly-once behavior: hold that ambiguous recovery and report it
rather than silently choosing an item or resending.

Readable exhausted absence is still unknown acceptance, never a proven rejection. Retain
the attempt and input hold and check again at the existing inventory polling cadence.
Active-turn history visibility/retention is unestablished; neither an active read's missing
message nor its reduced items means the request was rejected. A definite receipt can
confirm if actually observed in a complete valid scope; no test should assume it becomes
visible while the turn is active. Malformed/mismatched success replies, arbitrary errors,
timeout/disconnect, and failed durable confirmation writes remain sent-unconfirmed.
Only T6's exercised obsolete-expected-turn -32600 classifier authorizes proven rejection
and a fresh gated attempt. History adds no rejection class or speculative retry path.

## Receipt-local normal-end proof for a missed Stop

T4 `main.completed_turns` records accepted authoritative main completions, not the revision
at which old history happened to be reread. Writing a history end there would invent its
causal provenance. Declare one optional receipt-local relational proof, required only
when the exact input has a matching history receipt but no qualifying recorded T4 end.

```text
InputHistorySettlement = {
  kind: 'accepted-input-in-normal-completed-turn',
  thread_id: string,
  turn_id: string,
  item_id: string,
  admission_revision: positive integer,
  receipt_revision: positive integer,
  revision: positive integer,
  evidence_source: 'authoritative-root-history'
}

InputReceipt += {history_settlement?: InputHistorySettlement}

{type: 'input/history-settled', client_message_id: string,
 thread_id: string, turn_id: string, item_id: string,
 normal_end: {status: 'completed', error: null, items_view: 'full',
              source: 'thread/read' | 'thread/turns/list',
              matching_item: {type: 'userMessage', id: string,
                              client_id: string, input: TextReceiptInput},
              turn_scan?: ReceiptHistoryScan}}
```

This event follows an exact stored history receipt. Independently validate the native
normal completed Turn for that same exact root/turn, with no error, full authoritative
items that include that exact item/client/input, and no conflicting turn evidence. The
native normal completed Turn encompassing the admitted accepted input supplies the
relation that its own end follows that acceptance; the listener's observation revision
alone is not that relation. A completed status elsewhere, timestamps, item arrival order,
another turn's normal end, child/native supplementary Stop, or controller assertion of
waiting cannot substitute. Reduced/partial items, failed/interrupted turns, and unknown
evidence cannot settle. Exact full `thread/read(includeTurns:true)` corroboration is
exercised; if using paginated turns, exhaust the relevant exact-root scope and request
full items. Summary defaults and schema declarations alone do not establish full native
history or this relation. Unsupported relations remain accepted and held.

`matching_item.id` must equal outer item_id and the stored history receipt's item;
client_id must equal outer client_message_id; input must match the same durable immutable
input under the exact comparison above. For thread/turns/list, require turn_scan for the
whole exact root (`turn_id=null`), exhausted from null with full items requested; it is
the turns scan, not an items cursor. For thread/read, require a validated exact-root full
Turn containing that matching item. These are normalized claims from actually read native
evidence, not an authorization to construct a normal end from a matching receipt alone.

The listener validates admission_revision against this input's immutable T4 receipt,
receipt_revision against its InputHistoryReceipt, exact shared root/turn/item identity,
and `admission_revision < receipt_revision <= revision <= snapshot.revision`. These are
storage provenance plus explicit relational evidence, not a fabricated native Stop
revision. Store the proof and settle only that receipt atomically. Replaying it is a
no-change operation; contradictory proof is rejected. If a valid recorded T4 normal-end
revision already settles the input, this extra proof is unnecessary.

In particular, matching history acceptance for old turn A during current active turn B
confirms A. Independent normal-own-end evidence above can settle A despite a missed A
Stop. Do not emit `turn/completed`, change main.status/turn_id/seen_turns, append A as
latest, insert or revise main.completed_turns[A], create a SessionRecord/waiting marker,
qualify a root command, or stop B. Existing main.completed_turns stays solely under T4
accepted lifecycle provenance. This is an explicit narrow T7 alternative to T4's ordinary
settlement rule; root publication and implementation authorization remain required.
History receipt without that proof only accepts A and retains its input hold.

## Native idle routing is separate from normal main Stop

Declared clarification: T6 permits same-root idle continuation and calls for fresh main
selection, but its `delivery/sent` declaration does not explicitly say how the listener
admits that continuation when a missed Stop leaves durable main active/unknown. Declare
that a fresh authoritative current exact-root native `thread/read` whose returned Thread
has `status.type='idle'` can supply input-routing authority under the shared Gateway lock.
It does not supply normal-main-end or parking authority. Complete owned inventory and
history are different scopes: their uncertainty cannot prove idle, but need not negate
an independently valid exact-root current idle response or prevent delivery of an already
established eligible available outcome. This is a T6/T7 declaration clarification,
not a claim about existing implementation.

Use a minimal optional field on existing `delivery/sent`, without a selection callable,
cached idle flag, or another snapshot collection:

```text
NativeIdleSelection = {
  source: 'thread/read',
  thread_id: nonempty string,
  status: 'idle'
}

{type: 'delivery/sent', observed_revision: integer, batch_id: string,
 attempt_id: string, client_message_id: string, method: 'turn/start',
 thread_id: string, expected_turn_id: null, selection: NativeIdleSelection}
```

The Gateway holds its common input lock while reading the current exact launch revision,
performing that native read against the same live backend, selecting the root and asking
the listener for admission. Here observed_revision is captured before the native read and
must still equal the listener's current revision at admission. The normalized selection
comes from an actually received valid current read of the exact binding.conversation_id;
selection.thread_id, outer thread_id and the returned native Thread.id must all agree.
No cached/resume/history idle status, exhausted items, lack of a turn, latest selector,
notLoaded/systemError, malformed reply, or incomplete read is equivalent. A contradictory
current active native observation or intervening lifecycle/input revision invalidates
the selection; re-read under the lock. Native current thread status, not completeness of
old turn/item history, supplies this positive routing observation.
Retained main active A from before the missed end is not by itself a contradictory new
native observation; leaving that lifecycle record unchanged is the purpose of this seam.

The listener still atomically validates exact launch/root, live service, unchanged
revision, fresh existing T6 TaskState/StageSignal gate, immutable unconfirmed batch,
new attempt/client identity and every idle turn-owning receipt barrier. Every existing
input that could own/create a turn must independently be settled/rejected before this
start; a matching history receipt alone does not settle it. A receipt-local normal-end
proof above can resolve a missed-end barrier without changing main. Prior sent-unconfirmed
attempts never gain resend permission. This extension permits one newly admitted safe
same-root continuation despite main.status active/unknown when the exact native idle
proof satisfies these conditions. It retains ordinary admission's pending input hold and
stopped-evidence invalidation before forwarding. Missing/uncertain proof holds the start.

Do not translate selection into `turn/completed`, main.status stopped, completed_turns,
SessionRecord, waiting marker, command qualification, or empty work. Do not reseed away
recovered outcomes. Subsequent main lifecycle changes require their own actual native
events; current main normal Stop remains required for automatic park/cap retirement.
The schema/native facts distinguish idle from active/notLoaded/systemError and exercise
exact-root reads, but do not verify this particular listener admission extension or all
provider races. The isolated criterion below must verify it before implementation closes.

## Reconnect, terminal readiness, work and independent batches

With service live, control-client reconnect/reinstantiation reuses the same app-server,
Gateway, remote TUI and exact conversation. It initializes a new control connection and
uses exact-root thread/resume without input to subscribe to future lifecycle; read alone
is not subscription on the exercised version. Ordered recovery applies authoritative
active state before subsequent notifications through existing `turn/recovered` rules.
Old completed history never becomes current normal Stop. Root-unbound uncertainty holds
root/input forwarding and terminal readiness; it does not create a replacement backend.

Terminal readiness retains the design's existing valid initial reply or accepted bound
turn/started routes. An accepted start while the durable initial request is pending and no
later input has been admitted records optional `main.initial_start` provenance: exact initial
client ID, immutable admission revision, owning turn ID and accepted start revision. A later
attachment can use that historical proof without accepting or settling the receipt. A new
recovered turn, pre-admission start, stale/foreign start or later competing admission is not
proof; absent legacy metadata stays unknown. Once established, the initial proof survives
reconnect, later foreground admissions and later main turns for this same launch and root.
The snapshot validates its initial identity, admission revision and turn/revision bounds.
If both routes are missed, the exact initial client/input history receipt
releases readiness for the same root, even when its accepted turn is completed, failed,
interrupted, or older than a current active turn. Acceptance/readiness is separate from
normal receipt settlement. No bootstrap/root/prompt repeats and no unrelated history
unblocks readiness. Recovery of the unknown root ID itself remains unestablished.

Reconnect reconciles validated all-depth owned ancestry and every relevant exact-thread
inventory/history scope through existing inventory events. Preserve baseline_turns and
baseline_workers for this physical launch, previously qualified command identity/owning
Stop, eligible child identity, seen_completions, and available outcomes. Recover exact
owned outcomes missed while offline without rebatching assigned completions. Partial
definite observations may be retained under certainty unknown. Missing work, missing
in-progress startup items, root-only empty lists, notLoaded/thread-not-found, incomplete
ancestry/pages, and unreadable history never invent success, empty inventory, worker end,
or launch-baseline emptiness. A controller restart is not permission to reseed current
outcomes as prelaunch work. Existing scoped opaque outcome cursors retain T5 limits; do
not assume any arbitrary checkpoint can recover all missed records.

Use the shared Gateway input lock after re-reading the durable launch. An existing
sent-unconfirmed batch never resends. Distinct newly available outcomes can still become
a new immutable batch and steer a freshly authoritative active exact-root turn under
the T6 gate/precondition even while an older receipt is unresolved. Idle turn/start holds
behind every pending/accepted input that could already own/create a turn. History/read
alone clears no unrelated hold. A proven T6 rejection allows only the rejected attempt's
safe fresh attempt, with new attempt/client IDs and fresh gate/main read.

Listener/dispatcher restart preserves the exact stored launch rather than creating a
physical session or replacing the TUI. A dead service follows existing Failed/Resume
rules; T7 neither restarts it nor reclassifies it as live control uncertainty. Existing
working-stage/task/ticket/signal, service, input, revision, forced-fence, help/CI/review,
retry/loop, cancellation and stage transitions remain authoritative. A historical receipt
can resolve only current-launch uncertainty; it does not enable new result send across
a changed task gate. T4 automatic retirement still requires authoritative current stopped
main, known complete work, all independent inputs/results resolved, and eligible current
task/binding/revision. Existing cap equality/strict-greater, foreground exemption and
explicit closure behavior remain unchanged; receipt/history/reconnect never reset clock.

## One pending uncertainty condition per logical batch

Declare the existing alerts list record a deduplicated batch-scoped condition:

```text
DeliveryUncertainAlert = {
  kind: 'delivery-uncertain', batch_id: string,
  status: 'pending' | 'resolved', message: nonempty string
}

{type: 'delivery/uncertain', batch_id: string, attempt_id: string,
 client_message_id: string, message: nonempty string}
```

Accept only for that current batch's exact sent-unconfirmed attempt/client. Create at
most one condition per batch; repeated checks, reconnect, dispatcher restart, new diagnostic
wording, or later attempts of that same batch do not create another pending alert. It
identifies the pending batch and explains why automatic resend is held. Existing alerts
and compatibility/control surfaces remain preserved. Once exact ACK/history confirms
the batch, atomically mark its condition resolved and retain the record; do not republish
it on stale replays. Proven T6 rejection resolves uncertainty for that attempt without
confirming the batch; another genuinely uncertain safe attempt can update the same batch
condition to pending, never append another entry. T8 owns operator presentation,
notification-once delivery and resolution notification/events; this contract declares
stored condition state only and does not invent that flow.
Root bootstrap uncertainty uses existing control/compatibility reporting, not a fabricated
result batch. Alert recording does not park, clear receipts, reset clock, or enable send.

## Compact criterion inventory for the isolated writer

After root publication and explicit writer activation, use public event/view, the existing
supervisor executable/Gateway, external fake codex app-server/remote TUI, Sessions and
dispatcher seams. No implementation helper, prior harness, or active-history timing is
assumed by these declared criteria.

| Criterion | Public observable result |
| --- | --- |
| Controller/dispatcher reinstantiation | Same physical launch/root/backend/TUI; durable workers/outcomes/checkpoint/batches/inputs/clock survive; no new prepare, seed or bootstrap. |
| Lost root-ID reply | Bootstrap root attempt already durable before thread/start; reinstantiation emits no second thread/start or initial input and remains unknown until exact correlated identity is recovered. |
| Initial prompt uncertainty | Pending receipt exists before initial turn/start; lost reply/start cannot trigger a second prompt; exact matching completed history releases same-root readiness, unrelated history does not. |
| Durable result uncertainty | Existing sent-unconfirmed attempt/input precede forwarding and survive listener/control restart; persistence-before-forward crash remains held even if send never occurred. |
| Lost ACK recovery | One submission; completed exact-root paginated item history with exact client/item/turn/input confirms only that attempt/batch/input; no extra root/turn/resend. |
| Ordinary operator provenance | Optional content/method/root/precondition is already immutable on the existing pending receipt before forward, at its admission revision. Text-only lost ACK recovers that receipt through exact scoped history without a new ledger, another admission or resend. Legacy missing content remains unknown. |
| Text normalization and access | Only missing versus empty text_elements is equivalent; exact text/type/order/IDs/pages remain required. Changed/nonempty spans or richer forms cannot be guessed as a text match. Richer operator input and legacy/Claude two-argument callers retain ordinary admission/ACK access even when optional recovery provenance is omitted. |
| Scope/pages/input identity | Foreign root, turn mismatch, absent/null clientId, malformed/missing cursor, changed direction, partial pages or changed input cannot confirm; native opaque cursor is passed unchanged. |
| ACK/rejection boundary | Exact correlated valid ACK confirms; malformed/wrong-turn success, timeout, arbitrary error and readable history absence remain uncertain. Only known T6 expected-turn classifier permits fresh attempt. |
| Native IDs/duplicates | No provider dedup assumption; repeated exact observation changes nothing; differing immutable content/receipt conflicts are rejected; two distinct native items under supposedly unique attempted ID remain ambiguous. |
| Independent held work | D stays pending while distinct E safely steers known active exact root; E's ACK/history clears no D/operator hold. Idle continuation holds behind unresolved turn-owning inputs. |
| Missed Stop A during active B | Receipt alone accepts A; independent full exact normal-own-end relation settles A with receipt-local proof. B/main.seen/latest/completed_turns/SessionRecord/wait marker remain unchanged. |
| Missed Stop plus known native idle | With A's Stop missed, durable main active/unknown, an eligible recovered outcome and every turn-owning receipt independently settled/rejected, fresh locked exact-root native idle proof admits one same-root continuation. No main normal Stop, completion-map entry, SessionRecord, waiting marker, empty-work claim or baseline reset is fabricated. Missing/contradictory idle proof holds. |
| Missed outcomes/owned work | Existing T5 identities/eligibility and complete scopes recover exact offline outcomes once; baseline prevents old-launch work, and absence/partial history never invents outcome or emptiness. |
| Uncertainty condition | Repeated polls/restart keep one pending batch condition; exact receipt ends only that condition. T8 later verifies operator notification/resolution presentation. |
| Clock/gates/death | Recovery preserves since/ever_reported; unknown holds ordinary/cap park; active main is exempt; forced closures/stage/task/ticket/fence rules win; dead service takes existing Failed/Resume. |

## Public composition and evidence limits

This spec-root contract accompanies [controller-attachment-public-declarations.md](t7-public/controller-attachment-public-declarations.md) and
[native-text-input-public-declarations.md](t7-public/native-text-input-public-declarations.md) in `t7-public/`. The exact controller ownership
context, readiness/closure contract, first-launch/default attachment distinction and
capability-absence observation are declared in the composition document. This contract
retains the approved proposal, specification, design, T4–T6 runtime contracts and exact
0.156.1 native schema shapes. Native schema fixtures used by acceptance must be vendored
portably; temporary intake/provider-probe paths are never runtime dependencies.

Native facts establish active steer ACK, exact completed-history client/input/item matches,
dropped-owned-ACK recovery on the same live backend without resend, the exercised obsolete
expected-turn rejection, and repeat client IDs persisting as distinct items. They do not
establish active receipt persistence timing, recovery after losing a root-creation reply
and every correlated root identity, backend restart/crash preservation, arbitrary retention
or checkpoint positions, changing multi-page snapshots, malicious ID collisions, or
other versions. Full-page/full-turn relation and receipt-local provenance above are public
declarations to be verified, not claims that T4/T6 already implement them. Unknown
evidence holds; no native/source/transcript shortcut or empty-state claim fills these gaps.
Optional ordinary input provenance is likewise a new backward-compatible declaration.
Native text missing/empty span equivalence is exercised; richer input's
semantic persistence and generic recovery are not. Ordinary access remains available
through the existing admission/ACK contract, with unknown lost-ACK recovery where needed.

The public [service/reconciliation supplement](t7-public/public-reconciliation-declarations.md),
[T7 task replacement](t7-public/public-task-wording-candidate.md) and
[intake README](t7-public/README.md) complete the portable declaration-only intake.
