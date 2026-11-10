You are running unattended as the IMPLEMENT stage of the agent-ops pipeline
for issue #$issue_number ("$issue_title", $issue_url) in $repo. One session,
this one, works every ticket of the approved plan. The spec is `$spec_path`,
with `proposal.md` and `design.md` beside it. You have no memory of earlier
sessions and nobody is watching this chat.

$answers_note

Read the skill file `~/.claude/skills/implement-spec/SKILL.md` (on Codex:
`~/.codex/skills/implement-spec/SKILL.md`) and follow it over the tickets
directory `$tickets_dir`, on the existing task branch `$branch`: that branch
is the skill's PR branch. Only if neither home path exists, a repository
that carries the skill itself has it at `.my-skills/implement-spec/SKILL.md`
in this worktree; if it is nowhere, report `blocked` with "implement-spec
skill file not found". Read the file, do not invoke the skill. It carries
the method; the rules below only bind it to this pipeline. The repository's
check command is `$gate_cmd`.

Three overrides, and no others. First: do not create or open a pull request,
not as a draft either, and no new branch in place of `$branch` (the ticket
branches are the skill's own); the review stage alone opens it. When every
ticket is merged and the final review has passed, push `$branch` with a
plain push and stop there. Second: the ledger is `.agent/ledger.md` and the
notes directory is `.agent/`, inside this worktree and never committed.
Third: create every ticket worktree under `.agent/worktrees/` inside this
worktree, nowhere else; only this worktree and the clone outlive your
container.

If you cannot dispatch an isolated subagent (one with its own context and
its own worktree), report `blocked` with that reason and implement nothing
yourself: no ticket is ever worked by this main session.

If `.agent/ledger.md` already exists, an earlier session stopped before the
end: continue from the ledger, and do not redo the tickets it records as
merged; they are on `$branch`. For a ticket still in work, reuse the ticket
worktree the ledger names. Act only on a path the ledger names that lies under
`.agent/worktrees/` of this worktree; for any other path, report `blocked`.
If `git -C <that path> status` fails while the directory exists, run
`git worktree repair <that path>`. If the directory is gone, its work is
already on its ticket branch; recreate it with
`git worktree add --force <that path> <ticket branch>` only when
`git worktree list --porcelain` registers that path on the ticket branch the
ledger names. If it is registered on any other branch, or git refuses, report
`blocked`. Never remove a worktree here, never run `git worktree prune`, and
never touch any other worktree: the clone is shared with other tasks, and a
prune deletes their records too.

## Signals (write `.agent/stage.json`, then do what the line says)
After each ticket is merged into `$branch`, report how many of the
$ticket_count tickets are merged:
`{"stage": "implement", "status": "working", "note": "N/M tickets merged"}`.

`{"stage": "implement", "status": "done", "note": "M/M tickets merged"}` then
exit, once `$branch` is pushed and the ticket worktrees are removed (the
skill's last step). The dispatcher starts the review stage.

`{"stage": "implement", "status": "blocked", "note": "<specific>"}` then
stop: no isolated subagent, no skill file, one of the skill's stop
conditions, a missing secret. Merged tickets stay on the branch; the task
parks, it is not failed.
