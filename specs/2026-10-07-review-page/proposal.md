# Review page: one interactive page for the plan gate and the questionnaire

## Why

At the plan review gate the operator reads a Markdown summary on the console and types an
answer. The summary is read-only: the open questions carry options and a recommendation, but
the operator must retype a choice as text, on a phone, and the session must parse it. The
questionnaire of the spec stage has the same shape and the same friction. Nothing keeps the
choices once they are typed: a second device, a later session, or the implement and review
stages have only the chat text to go on.

## For whom

The operator, who reviews plans and answers questionnaires from the console, mostly on a phone
over Tailscale. The plan, implement and review sessions, which read the operator's choices as a
file instead of parsing chat text.

## Goal

The operator reviews a plan or answers a questionnaire on one interactive HTML page in the
console, and every selection is saved as a JSON file in the task worktree that the plan session,
the implement session and the review session read.

## Non-goals

- The Telegram notification and its link do not change. Whether Telegram stays at all is a
  later decision.
- The request panel on the task page does not grow to fit the page; the page gets its own
  full-screen route instead. No `size` message.
- The answers file carries no option texts. The page is the record of the texts.
- No JSON review file with a fixed React panel, and no Claude artifact for Claude sessions
  (both rejected on 2026-10-06).
- No new sandbox: the iframe keeps `allow-scripts` only and the artifact CSP keeps
  `default-src 'none'`.
- The dispatcher does not validate question ids. A session that reads an answer it cannot map
  ignores it and reports it; the pipeline never blocks on a stale id.
- Checking that Codex on the box can dispatch isolated subagents (issue #161).
- Packaging `to-questionnaire` (Matt Pocock's skill, installed on the box outside
  `.my-skills.json`) is noted for a fresh box, not done here.
- A second retention rule for the answers files. The one retention rule of review artifacts
  changes from 30 to 7 days.
