===SIGNAL===
- `{"stage": "address-review", "status": "awaiting-ci", "run_id": <id>}` then STOP.
===STEP===
Then run `$verify_cmd`, signal `awaiting-ci` with
the run id it prints, and stop; on resume with a failing conclusion, fix
and repeat this step. The dispatcher caps these rounds and parks the task
past the cap.
