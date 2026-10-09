# T7 public controller composition — public contract

Approved T7 public contract, following verified T6 merge `e77f9ee3a15bc76a1941874abdcf356866f5e7ff` and docs closure `bc3a975803597a6458d808d5cda06361d2dd1bde`. These declarations define required behavior for isolated acceptance and implementation; they do not claim T7 is implemented or complete. The isolated writer starts only after Root publishes this contract and explicitly activates its owned worktree.

Declared module: `dispatcher.codex_supervisor`.

Companion [runtime receipt contract](../runtime-receipt-contract.md) and
[service/reconciliation supplement](public-reconciliation-declarations.md) declare the retained receipt and same-launch service-death boundaries.

```text
attach_controller(client, binding, arguments, *, backend_path, input_lock,
                  mode='attach')
    -> AsyncContextManager[ControllerAttachment]

mode = 'attach' | 'first-launch'

ControllerAttachment.wait_ready() -> awaitable[RuntimeBinding]
ControllerAttachment.wait_closed() -> awaitable[None]

arguments.model: nonempty string
arguments.effort: string                 # empty means native default
arguments.prompt: string                 # read only in mode='first-launch'
```

Default `mode='attach'` has the following retained contract: fresh local controller, exact-current public view, known-root resume subscription, retained durable work/receipts/clock, no root creation, bootstrap prompt, input admission for bootstrap, reseed or service revival. It does not read `arguments.prompt`. Unknown bootstrap/root remains held. No additional handle fields are needed.

## Default attachment lifecycle

The following entry, native subscription, readiness and closure rules apply to default `mode='attach'`; first-launch mode differs only in its explicitly declared bootstrap/baseline duties.

Enter the async context to construct and start a fresh controller attachment. Entry reads
and validates the exact-current public view before native requests or listener effects.
Invalid, missing, unreadable, foreign, replaced or retired state cannot authorize any
forwarding; entry fails safely and leaves retained state intact. A declared dead service
cannot be revived by attachment and follows existing Failed/Resume rules. A valid unbound
launch or missing bootstrap provenance remains unknown and held, with no root guess,
thread/start, bootstrap turn/start, seed or ready terminal. The host may retry reading
managed state; it may not prepare another launch to bypass uncertainty.

For a known exact root, initialize a new control connection and use exact-root resume
without input to subscribe. Retain same-launch bootstrap/inputs, worker identities and
eligibility/outcomes, history baseline/checkpoint, batch membership/attempts/immutable
native input, alerts and wait clock. Every entry creates fresh controller-local state
from the durable public view and native observations; no old controller or response task
is handed into the new attachment. No state comes from caller-assigned controller fields.
All mutations remain ordinary approved listener event/input routes. No host snapshot or
TaskState write, new prepare, bootstrap admission or baseline reseed is part of attachment.

`wait_ready()` waits for this same launch's legitimate readiness proof: the retained
valid initial acceptance/bound lifecycle route or the approved exact immutable initial
client/input history receipt for the same root. Receipt readiness does not require normal
completion and does not fabricate Stop, settle unrelated inputs or select a newer root.
It returns a complete current binding for the same launch/root when ready. A lost unknown
root or unsupported/missing proof remains held. A caller may impose an outer timeout.
Cancelling a readiness waiter alone does not close the attachment.

`wait_closed()` observes completion of this attachment's owned observer and cleanup;
transient socket disconnect/reconnect or unknown evidence does not mean closed. Fatal
attachment failure is exposed to the waiter and context owner. Explicit context exit
completes closure. If closed before readiness, readiness waiters terminate with failure,
not an invented ready binding. Cancelling a closure waiter alone does not stop the native
backend or terminal.

Exit the context, including cancellation, to stop and await only this attachment's control
observer, callbacks, listener invocations and independent receipt tasks. On completed
exit there is no surviving owned observer/callback activity. An admitted/forwarded request
may remain durably sent-unconfirmed when its reply is lost; preserve that hold for approved
reconciliation. Never wait for a missing native ACK to infer rejection, roll back a durable
admission or authorize resend. Exit neither closes Gateway terminal connections nor kills,
replaces or declares dead the backend/TUI. Existing backend/service death and dispatcher
closure remain independently authoritative.

The owning host serializes replacement: await complete exit of A, reread the exact-current
binding, then enter B with the same endpoint and Gateway.input_lock. One active attachment
per owned launch is the host composition contract. Native requests already forwarded by A
remain the same physical attempts; B may observe their receipts but cannot resend them.
Gateway terminal traffic continues on its independent connection throughout the handoff.
The shared lock continues to order ordinary/result admission and forwarding. Lost/delayed
ACK waits cannot hold that lock or block inventory and distinct eligible active deliveries.

## First-launch lifecycle

Explicit `mode='first-launch'` is the production first-launch observer composition moved behind this same supported ownership context. It consumes the ordinary stage prompt from `arguments.prompt` and follows the declared durable T7 root-attempt and initial-input admission gates. The mode flag is a caller's requested operation, never proof of an unattempted root/input or permission to retry uncertainty. New T7-managed Codex launch preparation durably initializes `bootstrap: null` as explicit unattempted provenance; absent legacy state remains unknown and is never retrofilled. Only this positive listener-managed first-launch provenance and the recorded later root/input attempt states can authorize the first root request, inventory baseline and first input. Mode, service status, revision or record absence alone supplies no permission. Record each approved durable attempt before its forwarding; an ambiguous listener response or an existing attempted-unconfirmed record permits no repeat. A missing optional bootstrap record on retained state is not proof of no prior attempt. An explicit prepared resume selects exactly the recorded conversation; no newest-root/path heuristic.

Initial input is exact ordinary `BootstrapInput` text, not a T6 completion batch. Readiness comes from the same legitimate accepted bootstrap/lifecycle/history proof as the attached mode. First-launch mode can remain held when root/acceptance is unknown; repeated context entry cannot manufacture another initial prompt. Current listener remains sole snapshot writer; no new callable/HTTP mutation/state collection is added beyond the separately declared T7 bootstrap events.

The declared positive provenance is a **new T7-managed Codex prepare** that durably initializes `bootstrap:null`. Explicit null is the declared unattempted state; missing `bootstrap` on legacy retained state remains unknown and is never retrofilled. The transition from null to the immutable `bootstrap/root-attempted` record is atomic under the exact binding/revision and occurs before the root RPC. Mode, revision, service status or mere absence alone do not prove no prior attempt. Existing non-Codex runtimes and ordinary operator admission remain compatible. Implementation must preserve this exact distinction.

Both modes own and await only their controller observer, outstanding listener effects, callbacks and receipt tasks. Context exit, including cancellation, completes quiescent closure and preserves sent-unconfirmed holds. It does not stop the native backend, close the real Gateway/TUI, retire a launch or publish service dead. Service lifecycle remains the existing owner/CLI responsibility. Handle cancellation/readiness/closure rules are those declared above.

## Production and public fixture composition

Before Gateway creation there is no terminal input connection, so the owner supplies a real `asyncio.Lock()` to the first-launch context. It waits for the returned complete binding, exits first-launch context fully and waits for closure. Only then it constructs the unchanged public `Gateway(backend_path, root, client, binding)`. The Gateway creates its existing `input_lock`; no injection or field assignment is needed because the bootstrap observer is already quiescent and no terminal could race its input.

The owner enters default attachment A with that exact Gateway input lock, starts/keeps the Gateway server and real remote TUI, and exercises ordinary runtime behavior. It fully exits A, rereads the exact-current BoundClient view, and enters default attachment B with the same endpoint and same Gateway object/lock while backend/Gateway server/TUI all remain alive. Only A/B need share one lock throughout the handoff; the earlier no-terminal bootstrap context is a completed, separately owned phase.

Production `run()` uses this same first-launch context and subsequent default attachment composition. Its existing native backend/terminal lifecycle, native command shapes, gateway serve boundary, launch environment, listener authorization and service-death cleanup remain authoritative. A public fixture host can use the same callable and handles without recreating private bootstrap logic or accessing a Controller field/task.

The actual integration proves ordinary `Sessions` CLI reconnect separately. The fresh A/B phase uses a disclosed additional OWN in-container public composition launch. It never takes over the already-running CLI interpreter with a hidden command/endpoint or kills its native service to call that a handoff.

## Exact Touches

- `dispatcher/codex_supervisor.py`: one context-manager signature/handle and production `run()` composition; first-launch and attachment ownership reuse existing approved bootstrap/recovery logic.
- `specs/codex-background-lifecycle/runtime-contract.md`: declared first-launch/default attachment semantics.
- `specs/codex-background-lifecycle/t7-public/controller-attachment-public-declarations.md`: published exact callable/argument/handle contract.

No transport/Gateway signature, test-only endpoint, host mutator, snapshot collection, terminal replacement interface or operator/dispatcher API is required by this declaration. Acceptance Touches and approval are separate. Until root publication and implementation exposure of the declared signature, the v2 bootstrap initializer explicitly reports `first-launch-composition-unavailable` and cannot launch an A/B proof.

## Capability absence before implementation

Before this newly declared callable exists, an independently authored public fixture host
may report explicit `attachment-unavailable` when the existing module does not expose it.
This is an observable unsupported capability, not a simulated attachment or production
result shape. Evaluate it inside the behavioral test after healthy listener/provider setup,
retain its reason and clean up owned resources. Required real attachment entry/readiness
and replacement assertions fail on unavailable. No eager missing-name import, collection
failure, setup exception, skipped assertion, xfail, mock success or private-field fallback
satisfies this RED criterion. Native/listener setup failures retain their distinct errors.
Once the callable exists, the host must exercise it and the actual lifecycle contract.
Existing direct listener/event criteria remain independently testable on the retained base.

This observation requires root's explicit publication before lock. If the local isolation
workflow instead requires a callable interface scaffold, root must approve that separate
source checkpoint and reconcile the ticket base/gates before locking acceptance. No source
scaffold, base change or acceptance execution is authorized by this contract.
