# T7 declaration reconciliation — public contract

Approved T7 public contract, following verified T6 merge `e77f9ee3a15bc76a1941874abdcf356866f5e7ff` and docs closure `bc3a975803597a6458d808d5cda06361d2dd1bde`. These declarations define required behavior for isolated acceptance and implementation; they do not claim T7 is implemented or complete. The isolated writer starts only after Root publishes this contract and explicitly activates its owned worktree.

The public companion declarations are [runtime-receipt-contract.md](../runtime-receipt-contract.md), [controller-attachment-public-declarations.md](controller-attachment-public-declarations.md), and [native-text-input-public-declarations.md](native-text-input-public-declarations.md). Together with this supplement and [public-task-wording-candidate.md](public-task-wording-candidate.md), they are the complete declaration-only writer input after root publication and explicit activation. No source-private companion is part of that input.

## Same-physical-launch terminal service death

Retain the existing public listener event and service view, without a new mutation route or service-state collection:

```text
RuntimeControl.event(binding, event, *, now=None) -> bool
RuntimeClient.event(binding, event, *, now=None) -> bool
BoundClient.event(binding, event) -> bool

event = {type: 'service', status: 'unknown' | 'live' | 'dead'}
Snapshot.service = 'unknown' | 'live' | 'dead'
```

Once the existing owner records `dead` for an exact physical launch, later `live` or `unknown` service events for that same launch cannot revive it: reject them with false and leave the complete snapshot and revision unchanged. Identical same-launch `dead` replay is an acknowledged no-change operation and grants no forwarding permission. All events retain the existing exact-current binding, replacement and retirement fences. A separately authorized host prepare with a new physical launch remains allowed under existing Failed/Resume policy; reconnect or controller replacement is not prepare.

Both `attach_controller` modes read and validate the current public service state before native requests or listener effects. Already-dead state cannot authorize root start/resume, bootstrap/input admission or forwarding, baseline reseed, service-live publication or successful readiness. An already-dead attachment terminates with a public failure observable through context/readiness/closure rather than an invented ready binding; it preserves durable records. Death recorded during an attachment remains authoritative and cannot be reversed by later reconnect observations. Context exit itself does not declare death. The existing service owner retains native backend/terminal shutdown and dead publication; no new controller ownership policy is added.

Public criterion: record dead through the existing owner/listener boundary for one complete current binding, then attempt same-binding live/unknown updates and both context modes. Observe unchanged dead state/revision on rejected updates, no bootstrap/subscription/input forward or readiness revival, and quiescent attachment closure. Demonstrate an independently authorized fresh prepare has a distinct launch and does not inherit this same-launch terminal restriction. This is a declaration to be exercised later, not a source-derived behavioral finding.

Exact additional source Touches for this criterion are `dispatcher/runtime_control.py:_service` and `RuntimeControl._apply_current_event` for terminal/no-change listener semantics, and `dispatcher/codex_supervisor.py:attach_controller`, attachment entry/closure/readiness, `Controller.bind/start/connected/observe`, and `run` for the existing composition/service boundary. No public transport/Gateway signature change or new liveness callable is declared.

## Reconciliation constraints

New T7-managed Codex prepare writes explicit `bootstrap:null`. Retained absent bootstrap stays unknown; absence, mode, service or revision never authorizes first forwarding. Root-attempt and initial-input declarations are durable before forwarding, immutable and replay-safe. Only newly admitted operations can forward; no-change replay acknowledgment and ambiguous listener responses cannot become send tokens. The lost-root-and-lost-operation-correlation gap remains held and cannot be repaired by root guessing.

Optional ordinary `accept_input(..., native_input=None)` provenance remains on the existing input receipt/HTTP route. Omitted/null and two-argument calls preserve retained behavior. Immutable schema-valid supplied provenance is atomic with admission and cannot be upgraded later. Supported history comparison is exact text-only, with omitted versus empty `text_elements` equivalence; richer ordinary input retains admission/ACK access and unsupported lost-ACK recovery stays uncertain. Supplied `input:[]` is distinct from missing provenance.

History confirmation exhausts the exact fixed native item scope from null cursor through explicit final null, retains opaque cursors, and requires exact root/client/item/turn/immutable semantic input. Absence, partial/malformed pages or ambiguous matches do not prove rejection. Receipt-local full normal-own-end proof can settle that input without changing main lifecycle/completion provenance, SessionRecord, worker qualification, wait markers or clock. Native current idle selection is a separate exact-root `thread/read` observation under the shared lock, with listener revision captured before the read and unchanged at admission; old history is not idle authority. All existing T6 fresh TaskState/StageSignal gates, receipt barriers, immutable batch/new-attempt conditions and known-rejection classification remain required.

The supported `attach_controller` signature/handles and first-launch/default production composition are exactly the companion declaration. Default mode ignores the prompt and cannot bootstrap. Every context rereads durable state; completed exit awaits its owned observers/callbacks/listener effects/receipt work while preserving unconfirmed holds and keeping backend/Gateway/TUI alive. Replacement A must fully exit before fresh B uses the same Gateway endpoint/input lock. Production `run()` reuses this context composition. Missing new capability is observed within healthy behavioral setup and fails the required attachment assertions; import failure, skip, xfail, private-field fallback or simulated attachment does not satisfy them.

One existing-list `delivery-uncertain` condition is keyed by logical batch and survives polls/restarts/rewording/later attempts. Exact ACK/history confirmation resolves it; proven rejection resolves only the uncertain attempt without confirming its batch. Later genuinely uncertain safe attempt reopens the same condition. T7 supplies durable condition state only. T8 owns task audit and operator push/resolve presentation; no presentation callable or push token is added here.
