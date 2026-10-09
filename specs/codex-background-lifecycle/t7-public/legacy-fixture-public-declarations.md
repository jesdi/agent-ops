# Offline retained legacy fixture for T7

The isolated host-side fixture may create a retained legacy snapshot before starting
its listener or controller. This setup exception applies only to its owned temporary
state directory and the declared absent-bootstrap compatibility criterion.

1. Use the public host `RuntimeControl(state_dir).prepare(...)` or host
   `RuntimeClient.prepare(...)` to produce an otherwise legitimate Codex snapshot,
   then obtain that exact launch through the public view.
2. With all fixture listener/controller activity stopped and awaited, copy that
   returned snapshot and remove only the top-level `bootstrap` key, if present.
   Preserve every other field and value, including binding, revision, lifecycle,
   service, input/receipt/work/delivery state and clock. Do not substitute another
   runtime, mark an attempted launch unattempted, or create bootstrap:null.
3. Persist this JSON as the exact existing launch file. The public host layout is:

```text
key = SHA256(UTF8(target + NUL + decimal(issue))).hexdigest()
directory = state_dir / "runtime" / key
launch_file = directory / (launch_id + ".json")
current_pointer = directory / "current.json"       # {"launch_id": launch_id}
```

   The prepare-created current pointer stays unchanged. Write the owned launch file
   through an ordinary temporary-file/replace operation with finite JSON values.
4. Start the real listener and read the same launch through the public view. Assert
   that binding/revision and every retained field remain exact and bootstrap is absent.
   Exercise the declared compatibility behavior through real public events/input and
   controller contexts. Setup alone does not prove recovery, readiness or no resend.

During execution the host listener is again the sole runtime writer. The fixture must
never edit a snapshot concurrently with that listener/controller, write outside its
owned temporary directory, alter original task state, or use direct-file writes to
simulate acknowledgment/history/normal Stop. Keep the offline initial bytes and
public before/after observations as provenance. This declaration is fixture setup,
not a new production mutator, endpoint, legacy-upgrade permission or retry policy.
