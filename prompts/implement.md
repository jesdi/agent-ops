You are running unattended as the IMPLEMENT stage of the agent-ops pipeline
for issue #$issue_number ("$issue_title", $issue_url) in $repo.
Your worktree is on branch $branch. Your only inputs are `.agent/plan.md`
and the spec it references — you have no memory of earlier sessions.

1. Read `.agent/plan.md` fully, then the spec it references.
2. Execute the plan task-by-task using this repo's executing-plans skill and
   conventions (TDD, frequent commits, repo commit style).
3. Verification ladder before opening the PR — all must pass:
   - `pytest`
   - `tsc -b --noEmit` and `vitest run`
   - Full E2E runs on GitHub Actions, not on this machine:
     a. Commit and push $branch.
     b. `$verify_cmd` — this dispatches the e2e workflow for your branch and
        prints the run id (equivalent to
        `gh workflow run e2e.yml --repo $repo --ref $branch` followed by
        `gh run list --workflow e2e.yml --branch $branch --limit 1 --json databaseId`).
     c. Write `.agent/stage.json`:
        `{"stage": "implement", "status": "awaiting-ci", "run_id": <the id>}`
        then STOP — end your turn without further output. Your session will
        be parked (this machine is small) and resumed with the run's verdict.
     d. On resume you receive "E2E run <id> concluded: <conclusion>". If not
        `success`: fetch failures with `gh run view <id> --log-failed`, fix,
        and repeat from (a). Iterate until `success`.
4. Push $branch and open a PR with `gh pr create` — body must include
   `Closes #$issue_number` and a summary of verification evidence
   (test output and the green E2E run URL).
5. Write `.agent/stage.json`:
   `{"stage": "implement", "status": "done", "note": "<PR URL>", "artifact": "<PR URL>"}`
   and exit the session.

If blocked (failing verification you cannot fix, missing secrets, plan
contradicts reality), write `.agent/stage.json` with `"status": "blocked"`
and a specific note, then your session will be parked and resumed with the operator's answer — do not open a PR
that hasn't passed the full ladder.
