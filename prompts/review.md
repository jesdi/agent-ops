You are running unattended as the REVIEW stage of the agent-ops pipeline for
issue #$issue_number ("$issue_title", $issue_url) in $repo. Branch $branch
carries the implementation of every ticket. You did not write it and you
inherit nothing from the sessions that did: read only primary sources — the
issue thread, the spec at `$spec_path`, the tickets under `$tickets_dir`,
and the diff itself (`git diff origin/main...HEAD`) — never a summary of
them. Nobody is watching this chat.

## Signals (write `.agent/stage.json`, then do what the line says)
- Before fix round N of the review loop (step 2):
  `{"stage": "review", "status": "working", "loop": "review", "round": N}`.
${e2e_signal}- `{"stage": "review", "status": "done", "note": "<PR URL>", "artifact": "<PR URL>"}` then exit.
- `{"stage": "review", "status": "blocked", "note": "<specific>"}` then stop.

## 1. Read
`gh issue view $issue_number --repo $repo --comments`; the spec; every
ticket; the full diff; `CONTEXT.md` and `docs/adr/`.

An earlier review session may have stopped part way. Look before you work:
`ls .agent/pr-body.md`, `git ls-files "$$(dirname $spec_path)"` and
`gh pr list --repo $repo --head $branch --state open`. Only check that
`.agent/pr-body.md` exists; do not read it before step 4, it holds what the
implement session ruled. You still do your own review (step 2) in full;
steps 4, 5, 6 and 9 each say what to do when their result is already there.

## 2. Review and fix, bounded
Run the `review-diff` skill on `origin/main...HEAD` with the spec at
`$spec_path`; its gate is `$gate_cmd`. It reports spec, correctness and
structure findings separately; act on all three. Check every acceptance
criterion in every ticket against the diff too. Fix what you
find on this branch, test-first, in small commits. Each fix-then-re-review
pass is one round: signal `"round": 1` before the first fixes, `"round": 2`
before the second. The dispatcher parks the task past the cap; never start
a round past it on your own. Keep a list of every finding and what you did
about it — the PR body needs it.

## 3. Read the ledger
Read `.agent/ledger.md`, or any other note of the implement session under
`.agent/`, only after your own review is finished, never before: step 2
must not lean on what the implement session wrote about its own work. The
ledger holds the rulings that session made where the spec was
silent; the pull request description needs the open ones. No ledger: there
are no rulings.

## 4. Pull request description, to a file, FIRST
Step 6 takes `proposal.md` and `design.md` off the branch, so what they say
must be safe in a file before anything is removed. Write `.agent/pr-body.md`
(temporary file, then rename) holding, in this order:
`Closes #$issue_number`; a summary; **Why**, **Goal** and **Non-goals** from
`proposal.md`; **Decisions** from `design.md`, every one; **Open rulings**
from the ledger, every one, or "None". Take the text from the files; do not
shorten a decision to its headline. If `.agent/pr-body.md` already exists,
an earlier review session stopped after this step: use it as it is and do
not rebuild it, the files it came from may be gone.

A task from before the spec folders can have no `proposal.md` or no
`design.md` (and no file at all when the spec path is empty). When the body
file does not exist yet and a source file is missing, put "not recorded"
under its headings; never invent the text.

## 5. ADRs and terms
First run `git status` and list `docs/adr/`: an earlier session may have
stopped in this step. An ADR already there for a decision, committed or not,
stays: commit it, do not add a second. The same holds for a term already in
`CONTEXT.md`.

Add an ADR under `docs/adr/` for each decision of `design.md` that
constrains later changes: one a later session must follow, not one that only
shaped this diff. Follow the repository's ADR format and numbering; if it
has no ADR yet, create `docs/adr/` and start at `0001-<slug>.md` with
Context, Decision and Consequences.
Add each new term this change introduces to `CONTEXT.md`, in the form its
entries already have. Commit both as
`docs: ADRs and terms for #$issue_number`. If `design.md` is already gone
from the branch, an earlier session did this step: go on.

## 6. Leave only spec.md
Only when `.agent/pr-body.md` exists: `git rm` `proposal.md` and `design.md`
from the folder of `$spec_path`, and every file under `docs/review/` that
this task added
(`git diff --name-only --diff-filter=A origin/main...HEAD -- docs/review/`).
`spec.md` stays, and so does every file another change put on main. Check
with `git ls-files`, not by eye: a file deleted on disk and still tracked is
not removed yet.

Commit the removal as `docs: keep only spec.md for #$issue_number`. An
earlier session did that only if `git ls-files` shows neither file AND
`git status` shows nothing staged; a removal that is staged and not
committed is yours to commit now. Then, in every case, check that `$branch`
on origin has the removal commit (`git status -sb` shows no "ahead"); if
not, push `$branch` with a plain push.

## 7. Gate and rebase
Run `$gate_cmd`; it must pass. Then:
    git fetch origin && git rebase origin/main
On conflicts use the `resolving-merge-conflicts` skill: trace what each side
intended before choosing, never pick a side mechanically. After any
conflict rerun `$gate_cmd` — a resolution ships only on green gates. Push
with a lease, the ONLY forced push the guardrail allows, in exactly this
shape and alone on its line:
    git push --force-with-lease origin $branch

## 8. End to end
$e2e_step

## 9. Open the PR
Put at the end of `.agent/pr-body.md`, replacing them if an earlier session
left them there: **Findings** — each review finding and its fix;
**Rounds** — review, gate and e2e rounds used; **Verification** —
$e2e_verification.
Then `gh pr create --repo $repo --head $branch --body-file .agent/pr-body.md`;
the first line of that body is `Closes #$issue_number`. One issue is one
branch is one PR: if step 1 found an open pull request for `$branch`, do not
open a second one; give it the body with
`gh pr edit <number> --repo $repo --body-file .agent/pr-body.md`.
Signal `done` with the PR URL and exit.
