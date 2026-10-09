# Native text input receipt comparison — public contract

Approved T7 public contract, following verified T6 merge `e77f9ee3a15bc76a1941874abdcf356866f5e7ff` and docs closure `bc3a975803597a6458d808d5cda06361d2dd1bde`. These declarations define required behavior for isolated acceptance and implementation; they do not claim T7 is implemented or complete. The isolated writer starts only after Root publishes this contract and explicitly activates its owned worktree.

This supplement provides primary schema and captured native facts for the declared T7 history boundary in [runtime-receipt-contract.md](../runtime-receipt-contract.md).

Codex 0.156.1 `ThreadItemsListResponse.json` defines `UserInput` text with required `type` and `text`, and optional `text_elements` with default empty list. The exercised native receipt capture sends `[{type:"text",text:"LOCAL_FIXTURE_STEER_ACK"}]` and returns matching exact-root `userMessage.content` containing the same text plus `text_elements:[]`.

For the declared text-only immutable result/bootstrap input, receipt matching therefore compares native semantic input: list length/order, exact text/type, and text elements. Omitted `text_elements` and an explicitly empty list are equivalent. Missing/changed text, different type, nonempty/changed elements, extra input entries, unrelated client ID, native item or owning turn are not equivalent. Do not apply whitespace trimming, text rewriting, ordering normalization, or generic unknown-field dropping to manufacture a match.

This narrow equivalence confirms the matching exact input only alongside the declared T7 exact-root/client/item/turn and complete scoped history evidence. It supplies no turn completion, main Stop, idle authority, or proof that absent history means rejection. Ordinary operator history recovery uses the companion optional immutable native_input provenance on the existing input receipt; retained receipts without it stay recovery-unknown.

Primary evidence: exact-version generated `v2/ThreadItemsListResponse.json` definitions/UserInput; native receipt `concise-proof.json` ack.request.params.input and ack.itemsMatches[].item.content. Portable references name these public documents/schemas; installed temporary paths are not a runtime dependency.
