# Retained T4 public snapshot and input declarations

These public observables are already implemented and reviewed in T4. T6 extends delivery records without changing independent input receipt identity or settlement.

```text
Main = {
  status: 'unknown' | 'active' | 'stopped',
  turn_id: nonempty string | null,
  seen_turns: list[nonempty string],
  completed_turns: map[turn_id, positive integer revision]
}

InputReceipt = {
  status: 'pending' | 'accepted' | 'settled' | 'rejected',
  turn_id: nonempty string | null,
  revision: positive integer
}

Snapshot.inputs = map[client_message_id, InputReceipt]

InputAcceptedEvent = {
  type: 'input/accepted',
  client_message_id: nonempty string,
  turn_id: nonempty string
}

InputRejectedEvent = {
  type: 'input/rejected',
  client_message_id: nonempty string
}
```

`accept_input(binding, client_message_id)` atomically reserves a previously absent identity with status `pending`, null turn ID, and the positive revision of that admission's durable snapshot. It rejects reused identities or a foreign/replaced/retired binding. Every independent receipt remains separate.

An accepted receipt has a nonempty turn ID, even when its acknowledgment precedes the corresponding started notification. A rejected receipt has a null turn ID. Pending receipts also retain null. A normal accepted main completion records its exact turn ID and positive end revision; settlement requires that exact turn's end revision to be strictly after admission. A delayed acknowledgment for already-ended turn A can settle A during later active turn B using that durable mapping. It cannot settle another input or mark B stopped.

The current known main turn is the last unique entry in seen_turns. Active current turns have no accepted normal completion; stopped current turns have one. Unknown main state has null turn ID and retains history. Recorded completion and input revisions cannot exceed the snapshot revision. Turn-bearing history requires a bound conversation. Root historical reads alone do not manufacture a current normal Stop.

Main input acknowledgment is accepted via the public event shapes above. A successful `turn/start` result contains `turn.id`; a successful `turn/steer` result contains `turnId`. Explicit JSON-RPC rejection codes already distinguish nonacceptance from uncertainty. Unsupported/malformed acknowledgments remain pending. Native client-message-ID deduplication is not assumed.

Automatic retirement requires all ordinary input receipts to be settled/rejected and every available completion/delivery to have the required resolved state. Forced exact-launch retirement retains explicit stage/physical-closure authority. Result receipts must preserve these main-turn and per-input invariants while recording acknowledgment/history receipt separately from main turn completion.
