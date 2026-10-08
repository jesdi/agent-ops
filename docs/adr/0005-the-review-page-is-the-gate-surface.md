# 0005. The review page is the gate's surface; the answers file is its record

Date: 2026-10-07

## Context

The plan review gate showed a Markdown summary the operator read and
answered in text. The spec stage's questionnaire had the same shape. A
choice typed on a phone had to be parsed by a session, and nothing kept
it for a second device or a later stage.

## Decision

- The gate's artifact is one HTML page from the `review-page` skill
  (`.agent/review.html` at the plan gate, `.agent/questionnaire.html` for
  the spec stage's questions). The dispatcher judges it by shape only: a
  regular file inside the worktree, at most 256 KiB, carrying the template
  marker `<meta name="agent-ops-review" content="1">`. There is no Markdown
  fallback. The gate skip counts the page's `data-q` blocks.
- The console shows the page on its own route, in an iframe with
  `sandbox="allow-scripts"` and `srcDoc`. The console trusts a message only
  from that iframe's window, with `v` equal to 1. The page's buttons are
  the only approval.
- The operator's selections travel as one `answers` intent, debounced on
  the console. The dispatcher, never the web layer, writes the answers file
  (`.agent/review-answers.json`, `.agent/questionnaire-answers.json`) into
  an existing real `.agent/` directory, atomically, in the schema
  `{v, stage, submitted, submitted_at, actor, revision, answers}`.
  `dispatcher/answers.py` owns that protocol: the file per request kind,
  the submit vocabulary, the question-id grammar, the selection rule and
  the drop rules.
- A submission is bound to the page revision it was made on; a stale one is
  dropped. A draft never overwrites a submission of the same revision or
  one written from a typed reply. A new review round discards the previous
  round's answers file.
- A submission resumes the session with the file; a draft wakes nothing.
  The plan, implement and review sessions read the file as the operator's
  decisions. A session that cannot map an answer ignores it and reports it;
  the dispatcher does not validate question ids.
- Review artifacts of a terminal task expire after 7 days.

## Consequences

- A plan or spec session without the `review-page` skill blocks instead of
  falling back: a configuration fault that is meant to be seen.
- The page and the console move together through the skill pin; an old
  page on a new console shows a banner, it does not work.
- `restore` is sent once after the page's `ready`; a change on another
  device while a page is open is not pushed to it.
