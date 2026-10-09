# Host-only alert presentation

Implementation and genuine native actual-launch verification remain required; these
declarations make no implementation or integration success claim. Required selection,
refinement, claims and effects are in [runtime-presentation-contract.md](../runtime-presentation-contract.md).

Existing RuntimeClient(state_dir).event(binding,event,*,now=None) uses the host X-Runtime-Host credential header on POST /runtime/event. BoundClient.event uses the same existing route without that header. The host credential remains outside every container mount. Existing preparation and retirement already require the host credential; ordinary exact-launch controller/native events retain their established binding authorization.

For the declared alert/presentation-claimed event, require host authentication at that existing HTTP boundary. A container's exact launch binding alone cannot claim or suppress operator presentation; deny such a request with 403 and leave the record unchanged. Do not add a credential field to the snapshot, event body or container environment. RuntimeControl direct calls are trusted host boundary use; no extra public mutation callable is needed.

The selector covers both existing compatibility problems and final T7 delivery uncertainty:

```text
AlertIdentity = {kind:'delivery-uncertain',batch_id:nonempty string}
              | {kind:'compatibility',message:nonempty string}
{type:'alert/presentation-claimed',observed_revision:nonnegative integer,
 alert_identity:AlertIdentity,phase:'pending'|'receipt-resolved'}
```

Compatibility identity retains exact kind/message within the complete launch. Optional presentation metadata may be added to its existing record, preserving kind/message and existing deduplication; no new compatibility status or receipt-based resolution is implied. It can claim pending presentation once. Delivery identity retains exact batch within that launch; pending claims once per logical batch lifetime, and receipt-resolved only after its own accepted ACK/history confirms the batch and resolves its condition. A known rejection alone cannot announce delivered. No provider receipt resolves a separate compatibility problem.

At claim and before every effect, verify current saved in-flight/unparked task target/issue/worktree/continued-stage/ticket and current-launch association. No mandatory working StageSignal is added for presentation. Legitimate same-launch null-to-root refinement preserves admitted claim identity/metadata and original binding attribution; fresh events use the current complete binding. A replaced or physically retired launch cannot claim or notify against a replacement. A new task park/stop is not permission to send background results: presentation never admits result input, clears holds, ends a service, or changes task state. Preserve existing dispatcher loop/capacity policies.

The listener records a positive claim revision atomically before returning true; returns true only for a newly durable claim. Stale/repeated/foreign/unknown response authorizes no invocation. One same-pass dispatcher invocation follows that unambiguous new claim; recheck current association after asynchronous claim before the best-effort history/notifier effect. Record the ORIGINAL launch in event detail and notification context; a replacement race after final check must never relabel it as the replacement. Existing nontransactional external APIs cannot promise global exactly-once delivery or prevent every instruction-level race after authorization. Do not invent such a guarantee.

Operator inbound queues remain independent and unchanged. Dry-run makes no claims/effects. Durable runtime condition visibility survives missed best-effort presentation without automatic duplicate invocation. Final acceptance must prove host/container authorization, current-binding isolation and once-only behavior across restart.
