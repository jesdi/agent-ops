You are running unattended as the PLAN stage of the agent-ops pipeline for
issue #$issue_number ("$issue_title", $issue_url) in $repo.
Your worktree is on branch $branch. The approved spec is committed at
`$spec_path` — it is your only input; you have no memory of the spec session.

1. Read `$spec_path` fully.
2. Use this repo's writing-plans skill to produce a complete implementation
   plan. Write it to `.agent/plan.md` (it is gitignored — never commit it).
3. End with a self-review pass before signaling done: re-read the spec with
   fresh eyes and check the plan for coverage gaps and contradictions
   against it; fix anything you find.
4. Write `.agent/stage.json`:
   `{"stage": "plan", "status": "done", "note": "<one-line summary>", "artifact": ".agent/plan.md"}`
   and exit the session.

If blocked, write `.agent/stage.json` with `"status": "blocked"` and a note,
then wait. Do not implement anything in this session.
