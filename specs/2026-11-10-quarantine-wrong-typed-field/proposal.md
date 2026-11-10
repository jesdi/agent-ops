# A wrong-typed quarantine record no longer 500s /api/failures

## Why

`/api/failures` returns 500 when one quarantine record under
`state_dir/quarantine/` is readable, valid JSON and an object, but carries a
field of the wrong type — e.g. `{"blocker_issue": "abc"}` or
`{"blocker_repo": 5}`.

Root cause: `Sources.quarantine_entries()` (`web/sources.py`) guards only the
read and the shape of the record: `(OSError, ValueError)` around
`json.loads(p.read_text())` and `isinstance(d, dict)`. It then merges the body
over blank defaults and returns it unchecked. The `failures` route
(`web/app.py`) splats each entry into `read_model.QuarantineEntry(**q, ...)`;
Pydantic rejects `"abc"` for `blocker_issue: int` (and `5` for
`blocker_repo: str`) with a `ValidationError` that nothing catches, so the
whole route fails. A wrong-typed but truthy `blocker_issue` also reaches
`sources.issue_open(...)` before validation, asking GitHub about a nonsense
issue. Quarantine records are written only by `failures.write_quarantine`,
always well-typed, so the trigger is a hand edit or a partial/corrupted
write — but `/api/failures` is exactly the page the operator opens when
things are already going wrong, and the console's rule is that every
failure degrades to an explicit state, never a 500.

Reproduced by `tests/test_web_routes_read.py::test_failures_degrades_to_200_on_wrong_typed_quarantine_record`
(red on this branch: 500 instead of 200).

## For whom

The operator reading the console's failures view (and `/api/failures`
directly) while diagnosing the box.

## Goal

`quarantine_entries()` validates each record's field types at the
`sources.py` boundary and skips a malformed record exactly as it skips an
unreadable one, so `/api/failures` answers 200 listing every well-formed
record.

## Non-goals

- Surfacing unreadable or malformed records as an explicit "unreadable —
  still blocking" row (a `readable` field on `QuarantineEntry`, regenerated
  types, a `FailureList` branch). That is issue #87; this fix only makes the
  malformed case take the same path the unreadable case already takes, so
  #87 can later surface both at once.
- Coercing wrong-typed values (e.g. `"75"` → `75`). A record our own code
  never writes is treated as corrupt, not repaired.
- The dispatcher's `failures.check_quarantine`: it already treats a bad
  record as still blocking (an `issue_state` failure keeps it blocked).
  Its behaviour does not change.
- `fingerprint_entries()` (records under `state_dir/failures/`), which has a
  similar unguarded shape. Not reported, not in this issue.
- Any frontend or API schema change.
