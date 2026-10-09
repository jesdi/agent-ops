# Host runtime alert presentation

Implementation and genuine native actual-launch verification remain required; these
declarations make no implementation or integration success claim. Companion
declarations are [operator surfaces](operator-surfaces-public-declarations.md),
[host claims](host-presentation-public-declarations.md),
[host state](host-fixture-public-declarations.md) and the spec-root
[presentation contract](../runtime-presentation-contract.md).

## Callable

```python
# dispatcher.runtime_presentation
present_runtime_alerts(
    state_dir: str | Path,
    target: Target,
    issue: int,
    notifier: Notifier,
    *,
    dry_run: bool = False,
) -> None
```

`Target` is the existing `dispatcher.config.Target`; `target.name` selects the task and `target.repo` supplies its issue URL. `issue` is a positive integer. `state_dir` is the host's task/runtime/event state directory. The function reads the currently saved `dispatcher.state.TaskState`; a passed task object, snapshot or caller-selected alert cannot substitute for that current state.

`notifier` satisfies the existing `telegram.notify.Notifier.send(template: str, **ctx) -> int` interface. Production uses the existing notifier. Isolated execution uses an OWN recording notifier implementing that same external interface. A notifier return is an observed message ID or zero, not a provider receipt or reusable presentation authorization.

The function uses the real host `dispatcher.runtime_http.RuntimeClient(state_dir)` and its existing `view(target, issue, launch_id=None)` / `event(binding, event, *, now=None)` interface. The host credential remains outside task/container mounts. No injected claim callback, replacement history implementation, fabricated snapshot or new runtime mutation callable is accepted.

The dispatcher calls this same supported function for each task selected by its existing in-flight task phase, before its normal task drive. An isolated driver may call it directly for an OWN task whose state was saved through the existing task-state interface and whose launch/alerts exist in the real listener. Calling it does not run usage collection, operator inbound polling, GitHub/board operations, artifact publication, session launching or the rest of a dispatcher pass.

## Current task and launch eligibility

Dry-run produces no runtime claim, history append or notifier invocation. Missing, unreadable or invalid task/runtime evidence grants no presentation.

Eligibility uses a freshly read saved task under `target.name`/`issue`, with empty park and saved stage QUEUED, SPEC, AWAITING_SPEC_REVIEW, PLAN, IMPLEMENT, REVIEW, ADDRESS_REVIEW, BLOCKED or STALLED_ON_BUDGET. Its target, issue, worktree, continued stage and current implement-ticket cursor must match the current exact launch. AWAITING_SPEC_REVIEW continues SPEC; other saved stages retain their own continued stage. A non-implement launch has no implement ticket. The complete current runtime binding, including launch and conversation identity, scopes every new observation and claim. A physically retired, replaced or no-longer-associated launch is ineligible. No mandatory working StageSignal is added for presentation. Missing, unreadable or nonworking signal alone does not suppress otherwise eligible presentation. Control/inventory/main visibility need not be known to present an existing compatibility problem; presentation supplies none of that missing authority. The existing working TaskState/StageSignal gate remains result proposal/send authority only.

Fresh current task/launch association and phase eligibility are required immediately before the claim and again before each external presentation effect. A known change that breaks eligibility suppresses the effect. Legitimate approved null-to-exact-root binding refinement, with all other physical-launch fields unchanged, preserves association and existing claim identity/metadata; it creates no new pending claim/push. An admitted claim retains its original complete binding, including null conversation identity, in authorized effects. Fresh events use the current complete binding; the old null binding is not new event authority. A concurrently replaced launch is never relabelled as the replacement: any already authorized effect retains its ORIGINAL launch context. Nontransactional external interfaces do not promise elimination of every instruction-level race after the final check. Full refinement rules are in the spec-root contract.

## Sole mutation and once-only authorization

The only runtime mutation used by this entrypoint is the approved host claim through the existing `event` route:

```text
AlertIdentity = {kind:'delivery-uncertain', batch_id:nonempty string}
              | {kind:'compatibility', message:nonempty string}

{type:'alert/presentation-claimed',
 observed_revision:nonnegative integer,
 alert_identity:AlertIdentity,
 phase:'pending'|'receipt-resolved'}
```

The existing host `X-Runtime-Host` authentication is required. A container/no-header claim receives 403 without mutation. The listener remains the sole runtime writer.

Only an unambiguous boolean `true` for a **newly durable** current claim permits this invocation's effects. Duplicate/already claimed, stale, foreign, retired, unknown, rejected or ambiguous claims permit none. Reading an existing claim revision never recreates a send token. A failed/ambiguous claim response can leave a durable claim whose notification was omitted; later calls do not infer permission from it.

Delivery identity is exact complete launch plus logical batch, independent of diagnostic wording and attempt changes. Pending presentation can claim once during that batch's lifetime. A rejected attempt followed by safely reopened uncertainty cannot claim a new initial push for the same batch. Distinct batches and distinct physical launches remain independent.

Receipt-resolved presentation is allowed only when final T7 authority confirms that same batch from its own matching accepted receipt and its condition is resolved. A known rejection cannot claim delivered resolution. Compatibility retains `{kind:'compatibility', message:string}` and its exact kind/message deduplication, with optional pending-claim metadata only. It has no new status, receipt resolution or invented recovery lifecycle.

## Existing history and notification effects

Pending claim authorizes one best-effort task history append and one initial push invocation. Receipt-resolved claim authorizes one best-effort history append and **no push**. No intermediate rejection/reopen presentation is promised. If phase eligibility is known to change before a pending effect, no stale pending push is invoked.

The actual history interface is:

```python
# dispatcher.eventlog
append_event(state_dir, event: str, *, target: str = "", issue: int = 0,
             stage: str = "", model: str = "", actor: str = "dispatcher",
             detail: str = "") -> None
read_tail(state_dir, limit: int = 200) -> list[dict]
```

Event names are `runtime-alert-pending` and `runtime-alert-resolved`. Existing top-level event fields remain unchanged. `detail` is JSON text retaining the ORIGINAL complete binding, typed alert identity, phase and supplied diagnostic message; delivery entries include their logical batch and available attempt identity, and resolved entries state that the batch was confirmed by its own accepted receipt. Compatibility entries retain kind/message and never report receipt resolution. Diagnostic content is displayed as text. A caller can decode `detail` without parsing prose to compare launch/batch identity.

Pending notification uses `runtime_alert` through the existing `Notifier.send` interface and `telegram.templates.render` boundary, at existing `_TEMPLATES` registration location, with the existing task context keys `issue,title,url,note,target`. Issue URL is `https://github.com/{target.repo}/issues/{issue}`. The note identifies original launch, continued stage and ticket, typed alert and diagnostic. Delivery uncertainty additionally says receipt is uncertain, automatic resend is held, and exact bound-history reconciliation continues. Compatibility notification identifies the existing compatibility problem without inventing delivery uncertainty. The template does not announce a parked/failed task, free capacity, imply worker success or request agent input.

The callable returns `None`; observable results are the real listener view/claim metadata, existing event log records and notifier invocations. It returns no durable presentation token, provider acknowledgment or outcome assertion. History append and notifier are best effort and have no idempotent external transaction. A crash after claim, failed append, notifier timeout or zero can omit external presentation without retry; the durable runtime condition remains the visible authority. Ordinary presentation failures do not fail/park the task or authorize another push.

## Preserved authority and actual-driver use

Presentation does not admit or deliver agent input, resend results, settle receipts, create Stop/empty-work evidence, reset wait clocks, retire/end/restart a launch, write TaskState or mutate the operator-message queue. Existing stage, capacity, loop, cap and task transition behavior remains owned by the dispatcher and final T7 runtime contracts.

The actual isolated driver calls the public function with its OWN state directory, normal Target and task issue, and OWN recording notifier. It reads observables through real RuntimeClient and `eventlog.read_tail`; it also compares the existing inbound message queue before/after. Repeated calls, restarts, ambiguous claims, identical batch strings across tasks, resolution, known rejection/reopen, fresh launches, task replacement and dry-run must be observable through those public interfaces. No whole-pass invocation, unrelated network dependency, private helper access or replacement presentation algorithm is required.
