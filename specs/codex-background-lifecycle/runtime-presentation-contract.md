# T8 runtime alert presentation and actual-launch contract

This contract defines T8 behavior and required acceptance on the verified merged T7
source. Implementation, final branch review and genuine native actual-launch verification
remain required; this document makes no implementation or integration success claim.

## Supported host boundary

Use the existing runtime listener, saved TaskState, task audit/event log and Notifier.
The listener alone writes runtime snapshots; the dispatcher alone writes TaskState.
The supported host entrypoint is:

```python
# dispatcher.runtime_presentation
present_runtime_alerts(state_dir: str | Path, target: Target, issue: int,
                       notifier: Notifier, *, dry_run: bool = False) -> None
```

It reads current saved state rather than a caller-supplied task/snapshot/alert. It uses
the real host `RuntimeClient(state_dir)` and existing view/event routes, real
`eventlog.append_event`/`read_tail`, and `Notifier.send(template, **ctx)`.
The existing dispatcher in-flight phase calls this same function before normal task
drive. Direct use needs no whole-pass usage/inbound/GitHub/board/artifact/session flow.
No injected claim, runtime-view or history replacement is part of this interface.

`Target`, saved state, host client, event/history, notification and queue signatures
are declared in [t8-public/README.md](t8-public/README.md) and its companions. The
callable returns `None`; observables are real listener claim metadata, existing
task audit records and notifier invocations, with no durable presentation token or
provider acknowledgment returned.

## Retained conditions and identity

Retain compatibility alerts and final T7 batch uncertainty:

```text
{kind:'compatibility', message:string}
{kind:'delivery-uncertain', batch_id:string,
 status:'pending'|'resolved', message:nonempty string}
```

T8 observes T7's authoritative condition. It does not infer uncertainty or acceptance
from a timeout, notification return, inventory absence or prose. Logical delivery
identity is exact physical launch plus batch, independent of diagnostics, attempts,
issue-only keys, connection-local RPC IDs and list order. Distinct batches and distinct
physical launches remain independent, including identical batch strings across tasks.
Compatibility retains exact kind/message identity within that same physical launch,
its status absence and its existing deduplication. It has pending presentation only;
no receipt clears it or creates a compatibility recovery lifecycle.

Pending presentation claims once per logical batch lifetime. Repeated observations,
polls, restarts, wording changes or later safely reopened uncertainty create no new
initial push for that batch. Only T7 receipt/rejection authority changes condition
status. Receipt-resolved presentation requires that batch's own matching accepted
ACK/history receipt, a confirmed batch and resolved condition. Known rejection alone
cannot announce delivered. It resolves the attempt's uncertainty without confirming
the batch; a later safe uncertain attempt reopens the same condition. No intermediate
rejection/reopen history presentation is promised.

## Current task selection

The freshly saved task must match target/issue, have empty `park`, and be in the
existing in-flight set: `QUEUED`, `SPEC`, `AWAITING_SPEC_REVIEW`, `PLAN`, `IMPLEMENT`,
`REVIEW`, `ADDRESS_REVIEW`, `BLOCKED`, or `STALLED_ON_BUDGET`. Worktree and continued
stage must match the current launch. IMPLEMENT additionally requires its current
ticket cursor as the binding's ticket string; a non-implement launch has no implement
ticket. `AWAITING_SPEC_REVIEW` continues `SPEC`; other saved stages retain their own
continued stage. Retired, replaced or no-longer-associated launches are ineligible.
Missing, unreadable or invalid task/runtime evidence grants no presentation.

Presentation has no mandatory working StageSignal requirement. A missing, unreadable
or nonworking StageSignal alone does not suppress otherwise eligible compatibility
or delivery-condition presentation. Control/main/inventory visibility need not be
known to present an existing compatibility problem. The existing working
TaskState/StageSignal gate continues to govern result proposal/send. Existing
dispatcher stage, closure, cap, capacity and loop rules remain independently owned.

Fresh task/current-launch association and phase eligibility are required immediately
before the claim and before every external effect. Known park, stage, ticket, worktree,
replacement, retirement or phase changes that break eligibility suppress the effect.
Nontransactional effects do not promise elimination of every instruction-level race
after that final check.

## Same-launch root refinement and attribution

Every new claim uses the current complete binding and current revision. Legitimate
approved `conversation_id:null` to exact-root refinement, with target, issue, stage,
ticket, launch_id, runtime and worktree unchanged, remains the same physical launch.
It does not create another condition, invalidate an admitted claim, or grant another
claim/push for the same kind/message or logical batch lifetime.

An admitted claim's original complete binding is immutable attribution. If it contained
null conversation identity, retain that null in its authorized effects rather than
rewriting it to the learned root. Retained claim metadata remains valid through this
legitimate refinement and remains prior-admission evidence, never a reusable token.
A repeated already-claimed phase using the refined binding returns false.

Immediately before an effect, recheck current saved task/launch association and phase
eligibility. Legitimate same-launch refinement alone preserves association; the effect
still carries its original claim binding. Other contradictory binding changes or lost
association suppress the effect when known. A later replacement race never relabels
original attribution. Old null bindings cannot authorize new events after refinement;
later mutations use the current complete binding.

## Sole host mutation and newly durable claim

Use only the following event through the existing listener `event` route:

```text
presentation?: {
 pending_claim_revision?:positive integer,
 receipt_resolution_claim_revision?:positive integer
}
AlertIdentity = {kind:'delivery-uncertain', batch_id:nonempty string}
              | {kind:'compatibility', message:nonempty string}
{type:'alert/presentation-claimed', observed_revision:nonnegative integer,
 alert_identity:AlertIdentity, phase:'pending'|'receipt-resolved'}
```

Optional presentation metadata stays on the existing alert, separate from authoritative
condition status/receipt. Compatibility has only pending metadata and no status.
The listener atomically records the phase's positive durable claim revision and returns
true **only for a newly durable eligible claim**. Duplicate/already claimed, stale,
foreign, replaced, retired or phase-ineligible events return false without another claim.
Unknown/ambiguous/failed responses permit no invocation, even when later view reveals a
durable claim. Reading prior claim metadata cannot recreate a token or authorize retry.

The existing `X-Runtime-Host` header is mandatory for this claim on `/runtime/event`.
An exact-bound container/no-header request receives HTTP 403 with no mutation.
Trusted direct RuntimeControl calls remain host-boundary use. Ordinary native/controller
events retain existing authorization. The credential stays outside every container
mount and never enters snapshot, body or container environment. No new route, credential
field or runtime mutation callable is added.

## Best-effort history and notification

Only unambiguous newly durable true authorizes that invocation's effects. Pending
authorizes one best-effort `runtime-alert-pending` history append and one initial
`runtime_alert` push invocation. Receipt-resolved authorizes one best-effort
`runtime-alert-resolved` history append and **no push**. Eligibility is rechecked
separately before each effect; known phase change suppresses stale pending push.

Existing top-level event fields remain `ts,event,target,issue,stage,model,actor,detail`.
`detail` is JSON text carrying the ORIGINAL complete binding, typed alert identity,
phase and supplied diagnostic. Delivery records include batch and available attempt;
resolved detail states confirmation by that batch's own accepted receipt. Compatibility
retains kind/message and never reports receipt resolution. Diagnostic text is displayed
as text, preserving structured identity without requiring prose parsing.

Notifier context retains `issue,title,url,note,target`; URL is
`https://github.com/{target.repo}/issues/{issue}`. The note identifies original launch,
continued stage/ticket, typed alert and diagnostic. Delivery uncertainty says receipt
is uncertain, automatic resend is held and exact bound-history reconciliation continues.
Compatibility identifies its own problem without inventing delivery uncertainty.
The template does not announce park/failure, free capacity, imply worker success or
request agent input. A notifier return is an observed message ID or zero, never a
provider receipt or reusable authorization.

Claim-before-effect provides at-most-once invocation across restarts, not crash-atomic
exactly-once external delivery. Crash after claim, append failure, notifier timeout or
zero may omit presentation without automatic retry. Durable condition visibility remains
available. Presentation failure does not fail/park the task, clear a hold or authorize
another push. Dry-run makes no claim, history append or notifier invocation.

## Preserved authority

Presentation does not admit/deliver agent input, resend results, settle receipts,
fabricate Stop/main/SessionRecord/idle/empty-work, reset wait clocks, qualify workers,
retire/end/restart/revive a launch, write TaskState or mutate the inbound message queue.
Preserve T4–T7 receipts, clocks, work identities, baselines/checkpoints, immutable
batches/attempts, fences, existing background-cap note and dispatcher policies.

Owned host composition uses the declared saved TaskState/Target, real listener,
real event history and an OWN recording notifier external boundary. It calls the same
supported presentation entrypoint without a replacement presentation algorithm or
unrelated whole-pass network flow. Required observable criteria and the retained
actual-launch contract follow below; declarations are not an acceptance result.

## Required operator observables

| Situation | Required observable result |
| --- | --- |
| Pending D, repeated uncertain checks | One durable task/launch/batch runtime condition; one fresh claim authorizes at most one history append and initial notification invocation. D stays visibly pending; no resend, task park or clock reset. |
| Controller, dispatcher and listener restart | Same D identity/claim survives; another pass does not repeat presentation invocation. A lost claim reply, crash between claim/effect, append failure and notifier 0 leave external effects unproved without retry. |
| D receipt with E and operator input pending | D alone resolves and its own receipt-resolution claim permits one best-effort history append; E/input/compatibility holds remain; receipt supplies no Stop or retirement authority. |
| Duplicate/stale ACK and history observation | No second confirmation, initial notification, delivery or main turn. |
| Proven rejection then safe uncertain attempt | Retained runtime condition/attempt records show nonacceptance and renewed uncertainty; no promised intermediate history append, false delivered message or repeated initial notification. |
| Two tasks with identical batch strings | Independently scoped conditions, notifications and receipts; A's receipt cannot change B. |
| Fresh launch/replacement ticket/same conversation | New identity; old callbacks cannot notify or resolve the new condition; retained old history remains launch-scoped. |
| Replacement racing notification | Check association at claim and immediately before effect; known replacement suppresses invocation. Any effect racing later replacement retains ORIGINAL launch context; no instruction-level race prevention claim. |
| Compatibility/control visibility lost | Exact kind/message alert retains shape/dedup/status absence plus optional pending-claim metadata; one pending invocation across restart, no receipt-based clearing or receipt-resolution claim. No fabricated Stop, receipt or empty work. |
| Legitimate same-launch null-to-root refinement | Current events use refined binding; prior compatibility condition/claim retains identity and original attribution, including null, with no repeated pending claim/push or invalidated claim metadata. |
| Presentation selection without working StageSignal | Valid current saved in-flight/unparked task/launch association can present the existing problem despite missing/unreadable/nonworking signal; result send and normal stage/closure authority remain unchanged. |
| Claim host authorization | Host RuntimeClient header can claim; exact-bound BoundClient/no-header request receives 403 with no mutation and cannot suppress notice. No credential enters task mounts/environment/body/snapshot. |
| Supported direct host entrypoint and production composition | Real saved state/listener/event log plus OWN recording notifier observe the same presentation callable reused by the dispatcher, without whole-pass unrelated network flow or replacement presentation algorithm. |
| Normal dispatcher behavior | Existing cap note, queues, stage/help/CI/review/retry limits and writer ownership remain observable. |

## Required actual-launch acceptance

All items require the verified merged T7 source and final public contracts. Document
publication or command construction alone does not satisfy them.

- Pin the source revision and genuine Codex 0.156.1 / Claude 2.1.288 binaries. Use
  isolated homes and generic local scripted providers with no auth, real API calls or
  private-payload export. Disclose every owned wrapper, environment override, local
  provider endpoint substitution and fault-injection transport. The genuine CLI/TUI,
  native messages and real container launch remain on the exercised path.
- Launch owned fixture tasks through actual Sessions → herdr → Podman, with isolated
  task/state/worktree identities. Observe two concurrent tasks, capacity retention,
  fresh launch and replacement tickets; no latest-conversation selector or cross-task
  input/outcome/alert. Do not touch foreign resources, #154 or live tasks.
- Attach the real terminal to the exact already bound root. Submit native operator
  input while an owned result arrives; observe its active-turn routing and the same
  terminal/root/backend during idle continuation, control disconnect and recovery.
  An attached preflight screen or manually reconstructed launch is insufficient.
- Create genuine owned background commands and actual spawned descendants through
  depth 3, including a descendant-owned command with its exact qualifying owning Stop.
  Observe all-depth ancestry validation, successful and nonzero outcomes, and an actual
  child-agent error. Foreign/internal threads and native supplementary child messages
  cannot become main Stop or a second controller delivery.
- Exercise control-client disconnect while service/TUI remain live, offline owned
  completion recovery, a lost owned ACK with exact native history recovery, and an
  unresolved receipt that produces the visible notification-once condition. Keep
  independent pending inputs/results separate; preserve wait clock and baseline.
- Restart only the owned listener, verify a newly created socket inode, and reconnect
  through the existing read-only directory mount. Demonstrate actual Podman enforcement:
  native clients can connect but cannot create, remove or replace that endpoint from
  the task container. Check primary/secondary-provider task mounts and the actual
  applicable setup/triage bind construction for private-state aliases.
- Verify every emitted mount's canonical host source: private runtime snapshots and
  host credential are outside all mounts, including worktree/clone, provider homes,
  supervisor/dependencies and supporting config. Container public socket access never
  reads host snapshots/token. Read-only mounts are still mounts for this exclusion.
- Execute Claude's installed native hook commands with actual native session/prompt/
  Stop/child payloads under launch identity. Observe stale/foreign/child isolation,
  native admission success/rejection behavior and existing Claude continuation rules.
  Handwritten hook objects or constructed command strings do not establish this item.
- Exercise owned Codex service death separately from control disconnect: existing
  Failed/Resume behavior wins, no automatic service/root replacement, and cleanup
  terminates only owned descendants/containers/herdr sessions/listener/provider/home
  resources. Verify cleanup after success and failure; record ownership identities
  before teardown so foreign/live resources remain untouched.
- Run `make gate` for authorized implementation, final required branch review and
  review-ready PR preparation. Preserve cap equality/strict-greater, foreground
  exemption and explicit closure rules. Deployment and live resumes remain excluded.

## Box access and remaining authority

User Tailscale sign-in is complete and box access/baseline has been verified. The earlier
sign-in blocker is cleared. Access/baseline verification does not establish final T8
integration or any actual-launch criterion on the finished verified T7 base. Final
implementation and actual-launch verification remain required. Access/baseline
verification does not establish that final box integration passed.

Retained public references are the approved proposal/spec/design, final T4–T7 runtime
contracts, [public declarations](t8-public/README.md) and
the T8 task in `tasks.md`.
