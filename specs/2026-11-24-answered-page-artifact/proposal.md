# Answers artifact: the answered page, kept in the box archive

## Why

The `review-answers` and `questionnaire-answers` artifacts open as raw JSON, and
they can go missing. The diagnosis (reproduced by
`tests/test_answered_page_artifact_acceptance.py`) finds two causes in
`dispatcher/task_artifacts.py`:

1. **Raw JSON.** `_automatic_registrations` registers the answers files
   themselves (`.agent/review-answers.json`, `.agent/questionnaire-answers.json`)
   and the collector copies their bytes as the artifact content, with media type
   `application/json`. Nothing ever joins the answers with the page they answer,
   so the link shows the JSON the dispatcher wrote, not the review page or
   questionnaire the operator filled in.
2. **Capture depends on the worktree at collection time.** The answers are
   copied into the archive (`<state_dir>/artifacts/<key>/content/<id>`) only by
   the upkeep collector, which reads the file from the task worktree and only
   when the worktree is a directory. The answers file lives there for a short
   window: `answers.discard_stale` deletes it when the next request of the same
   kind is armed, and a broken or cleaned-up worktree (#280) hides it from the
   collector altogether. A submission whose worktree breaks, or whose next round
   arms, before the next collection is never archived, and the artifact is
   `404 artifact not found`. A later collection can also pair a newer page with
   older answers, since the session rewrites the page in the next round.

The archived copy itself already survives worktree removal (the console serves
from the archive, not from the worktree); what is missing is capturing the right
thing, at the moment it exists.

## For whom

The operator, reopening a plan review or questionnaire they answered, from the
task page's Artifacts panel, on any device, during the task and up to seven days
after it ends.

## Goal

Opening `review-answers` or `questionnaire-answers` shows the review page or
questionnaire that was answered, with the submitted answers filled in and every
input read-only; the link keeps working after the next round and after the
worktree is cleaned up or broken.

## Non-goals

- A history of every round: each artifact id shows the latest submitted round
  only; earlier rounds are not kept as separate artifacts.
- Drafts as artifacts: only a submission (`changes` or `approve`, or a text
  answer a session wrote) becomes the answers artifact.
- Rewriting answers artifacts already archived as JSON before this change; they
  stay as they are until their task submits again or they expire.
- The Send bar, delivery status and the raw-page banner of #281.
- Worktree git-metadata repair (#280).
- Changes to the review-page template or the answers file format.
