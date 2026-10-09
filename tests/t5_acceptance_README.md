# T5 independent acceptance boundary

The authoritative declarations are in
`specs/codex-background-lifecycle/runtime-contract.md`. The portable criterion
matrix is `tests/t5_acceptance_manifest.json`; each pytest case also records its
criterion IDs in JUnit properties.

Run the three `test_bound_turns_t5_*acceptance.py` files together. Runtime tests
exercise the public RuntimeControl API. Executable tests launch the actual
supervisor and waitd listener, then observe public RuntimeClient views. Task tests
use real Sessions and run_pass with independent external dependency doubles.

`t5_provider/codex` is a local executable fake. It implements the declared native
0.156.1 JSON-RPC rows, turns, notifications and acknowledgments over Unix WebSocket.
Pending commands are absent from persisted item history. Completed items retain
their original owner and turn. Thread discovery respects declared source and
ancestor/parent filters. All lists use one-row pages, scoped opaque cursors and
readable exhaustion. Requested limits are upper bounds. Native repeated input
client IDs are accepted independently, without invented deduplication.

The fake's separate fixture action socket schedules native state, faults,
disconnects and barriers. It never reads product snapshots or credentials and
never manufactures runtime events. Reduced native itemsView fixtures force
history pagination. Runtime state observations always come from the test parent
through the actual listener transport.

Executable fixtures derive the checkout and interpreter from their own location
and sys.executable. They own temporary homes, state, sockets and process groups;
cleanup releases their owned processes and removes their temporary directories.
No external provider, model CLI, messaging service or ephemeral intake directory
is required. Result transmission, receipts and re-instantiated-controller
recovery remain T6/T7.
