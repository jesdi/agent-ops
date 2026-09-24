===SIGNAL===
- `{"stage": "review", "status": "awaiting-ci", "run_id": <id>}` then STOP (step 4).
===STEP===
Run `$verify_cmd` — it dispatches the repository's e2e workflow for $branch
and prints the run id. Signal `awaiting-ci` with that id and stop; you are
resumed with "E2E run <id> concluded: <conclusion>". On a failure fetch the
logs (`gh run view <id> --log-failed`), fix, commit, push (a plain push, or
the lease push above if you rebased again), and repeat this step. The
dispatcher counts these rounds and parks the task past the cap.
===VERIFICATION===
the gate output summary and the green e2e run URL
