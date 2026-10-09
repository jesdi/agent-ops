# Existing operator surfaces — public declarations

Implementation and genuine native actual-launch verification remain required; these
declarations make no implementation or integration success claim. Required behavior
is in the spec-root [runtime-presentation-contract.md](../runtime-presentation-contract.md).

```python
# dispatcher.eventlog
append_event(state_dir, event: str, *, target: str = "", issue: int = 0,
             stage: str = "", model: str = "", actor: str = "dispatcher",
             detail: str = "") -> None
read_tail(state_dir, limit: int = 200) -> list[dict]
# telegram.notify
Notifier(dry_run: bool = False, console_url: str = "", multi_target: bool = False)
Notifier.send(template: str, **ctx) -> int
# dispatcher.main
Deps(github: object, sessions: object, notifier: object)
run_pass(cfg: Config, deps: Deps, dry_run: bool = False,
         config_path: str = 'targets.yaml') -> None
# telegram.templates
render(template: str, multi_target: bool = False, **ctx) -> str
```

Events are existing state_dir/events.jsonl objects with ts,event,target,issue,stage,model,actor,detail. Append is best effort: failures warn rather than raise into the dispatcher pass. The existing console history and operator audit read this log. No event idempotency key, confirmed append return or external transaction is part of the established boundary.

Task notifications supply issue,title,url,note,target context to the existing notifier. send renders a template and returns a positive Telegram message ID when obtained, otherwise 0 for dry-run/missing configuration/error. It has no durable idempotency token or delivery acknowledgment transaction. A timeout/0 cannot prove external nonacceptance. Acceptance uses an OWN recording notifier, never real Telegram credentials/network/messages.

Existing dispatcher.messages is an independent per-(target,issue) inbound operator-message queue. Its public Message is {id,text,actor,created_at,delivered_at}; append(state_dir,target,issue,text,actor)->Message, all_messages(...)->list, undelivered(...)->list and mark_delivered(...,ids)->None. Only dispatcher writes/drains it at session boundaries. Runtime background alerts must preserve this queue and its identities; do not insert alert output into it as input to the agent.

The listener remains sole runtime-state writer. Durable runtime presentation claim/state uses the declared host-only event through RuntimeControl.event and snapshot validation; the dispatcher does not write snapshot files. Final T7 pending/resolved delivery condition is separate from external notification delivery. Do not claim crash-atomic exactly-once history+Telegram delivery from these best-effort APIs. Preserve durable visible runtime conditions even if an external notification fails; never free input/result/main/retirement holds to make an alert disappear.

`run_pass` is the public dispatcher entrypoint. Existing internal `_run_pass` has the
same signature and owns the selected in-flight phase; it is declared only as an
existing Touches location, not a new public/test caller seam. Both production composition
and direct presentation use the supported `present_runtime_alerts` contract. Direct
presentation does not promise isolated execution of the whole dispatcher pass.

`Notifier.send` uses existing `telegram.templates.render`. The existing `_TEMPLATES`
registry is a Touches location only. Add the declared `runtime_alert` template at that
location with existing `issue,title,url,note,target` context; no new render/registry
callable or registration seam is declared.
