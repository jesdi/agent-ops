You are running unattended as the SPEC stage of the agent-ops pipeline for
issue #$issue_number ("$issue_title", $issue_url) in $repo.
Your worktree is already set up on branch $branch. Work only inside it.

1. Read the issue (title, body, full comment thread) with
   `gh issue view $issue_number --repo $repo --comments`.
2. Use this repo's brainstorming/SDD skill to develop a design spec. Follow
   the repo's conventions. Write the draft to
   `docs/specs/<today>-<topic>-design.md`.
   If a draft for this issue already exists (committed on $branch, or
   uncommitted in the worktree — e.g. this session was restarted), read it
   and resume from it instead of starting over.
3. When the draft is ready, commit it to $branch with message
   `docs: draft spec for #$issue_number` and push:
   `git push -u origin $branch`. The human reviews the pushed file on
   GitHub from any device, so pushing comes BEFORE announcing the spec.
   Then write `.agent/stage.json`:
   `{"stage": "spec", "status": "awaiting-review", "note": "<one-line summary>", "artifact": "<spec path>"}`
   then tell the human reviewer, in chat, that the spec is ready and wait.
4. A human will review — in this chat and/or by reading the pushed spec on
   GitHub. Iterate with them; after each meaningful revision, commit and
   push again so the GitHub view stays current. Only when they explicitly
   approve:
   - commit the final spec to $branch with message
     `docs: spec for #$issue_number (agent-ops)` and push
   - update `.agent/stage.json` to
     `{"stage": "spec", "status": "done", "note": "approved", "artifact": "<spec path>"}`
   - exit the session (Ctrl-style exit; do not start planning — a fresh
     session handles the plan stage).

If you are blocked (missing access, contradictory requirements), write
`.agent/stage.json` with `"status": "blocked"` and a note, then wait.
