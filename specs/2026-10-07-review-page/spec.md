# Review page: one interactive page for the plan gate and the questionnaire — Spec

Terms:

- The **console** is the web application in `frontend/`, served by `web/app.py`, that the
  operator opens over Tailscale.
- The **review page** is one self-contained HTML file a session writes in the task worktree:
  `.agent/review.html` by the plan session (plan mode), `.agent/questionnaire.html` by the spec
  session (questionnaire mode). It comes from the `review-page` skill in `jesdi/general-skills`:
  the skill's `template.html` owns the layout and the script; the session fills the content
  slots and never edits the script.
- The **template marker** is the line `<meta name="agent-ops-review" content="1">` that the
  template carries. The dispatcher looks for it; the number is the page format version.
- The **bridge** is the console code that exchanges window messages with the review page's
  iframe.
- An **answers file** is `.agent/review-answers.json` (plan) or
  `.agent/questionnaire-answers.json` (spec) in the task worktree, written by the dispatcher.
- A **question id** is the `data-q` value of one question block on the page, chosen by the
  session, lowercase letters, digits, hyphens and underscores, at most 64 characters.
- A **draft** is a set of answers sent before a button press (`submit` is `null`). A
  **submission** is a set sent by a button press (`submit` is `"changes"` or `"approve"`).
- The **snapshot** of a registered file is the copy the dispatcher takes into
  `~/agent-ops-state/artifacts/<target>/<issue>/content/` on each pass.

Message schema (version `v` is the integer `1`; every message carries it):

- Page to console, once, when its script listens: `{"type": "ready", "v": 1}`.
- Page to console, on every change and on every button:
  `{"type": "answers", "v": 1, "answers": {...}, "submit": null | "changes" | "approve"}`.
  The console adds the request's `revision` when it posts the set to the box.
- Console to page, after `ready` and whenever saved answers arrive:
  `{"type": "restore", "v": 1, "answers": {...}}`.

`answers` is a flat JSON object. A key is a question id, or a question id followed by `.note`,
or `track`. A value is a string (one chosen option id, a note, or a track name), a list of
strings (a multi-choice question), or a boolean.

Answers file schema (the dispatcher writes it; sessions read it; a session that takes a text
answer writes it in the same shape):

```json
{
  "v": 1,
  "stage": "plan",
  "submitted": "changes",
  "submitted_at": "2026-10-12T10:12:03+00:00",
  "actor": "jesdi",
  "revision": "9f2c…",
  "answers": {"debounce": "draft", "debounce.note": "flush on pagehide", "track": "standard"}
}
```

`stage` is `"plan"` for `.agent/review-answers.json` and `"spec"` for
`.agent/questionnaire-answers.json`. `submitted` is `null` for a draft. `submitted_at` is the
time of the intent that was applied, or `null` for a draft.

Unless a scenario says otherwise, the task is issue #412 "Export a portfolio as CSV" in
`jesdi/portfolio_eval`, on branch `agent/412-csv-export`, worktree `/srv/wt/portfolio_eval/412`,
on the `standard` track, and the operator's console login is `jesdi`. The review page of the
plan session has three questions with ids `format`, `limit` and `headers`, each with options
`a` and `b`, and the track pills `trivial`, `standard`, `security`.

## Requirements

1. **The plan session writes the review page, not the Markdown summary.** At the gate it
   writes `.agent/review.html` with the `review-page` skill in plan mode, registers it in
   `.agent/artifacts.json`, and signals `awaiting-review` with that path as the artifact and
   the number of questions as `open_questions`. It writes no `.agent/plan-review.md`, and the
   dispatcher no longer checks one.
2. **The spec session delivers its questionnaire as a review page.** It decides the questions
   with the `to-questionnaire` skill, writes them to `.agent/questionnaire.html` with the
   `review-page` skill in questionnaire mode, registers it, and signals `awaiting-answers` with
   that path. It writes no `.agent/questionnaire.md`.
3. **The dispatcher accepts a review page by shape only.** The artifact at the gate must be a
   regular file inside the worktree, at most 256 KiB, that contains the template marker. A
   page that fails any of these is bounced the way a bad summary is bounced today (the session
   is told what failed and retries; the stage fails after the same number of retries).
4. **The console shows the page on its own route.** `/task/<target>/<issue>/review` shows
   the review page of the open request, full screen, in an iframe with `sandbox="allow-scripts"`
   and `srcDoc`, with no other panel. The task page links to that route while a request is
   open. The request panel's own "approve plan" button is gone; the page's buttons are the
   only ones.
5. **The bridge trusts the iframe window and nothing else.** The console handles a window
   message only when its source is the review page's own iframe window and its `v` is `1`.
   A message from any other window is ignored. A message from the iframe with another `v`
   is ignored and the route shows "this review page needs a newer console".
6. **Saved answers go back to the page.** The console sends `restore` once after the page's
   `ready`, with the `answers` the request route returns: the answers file in the worktree,
   overlaid with the newest pending `answers` intent of the task. When neither exists,
   `restore` carries an empty object. The request route also returns the request's
   `revision`; when it changes while the route is open, the console reloads the page and
   says "the plan changed; your selections were reset to the saved ones".
7. **A selection becomes one intent, debounced.** The bridge collects `answers` messages for
   about one second and posts the last one to `POST /api/task/<target>/<issue>/answers`. A
   message with `submit` set is posted at once. On `pagehide` the pending message is posted at
   once. The route validates the body, writes one intent with action `answers` and payload
   `{"answers", "submit"}`, and returns 202 with the intent name, like the reply route. A
   body whose `answers` is not a flat object of the schema's types, or whose JSON is larger
   than 64 KiB, is refused with 422.
8. **The dispatcher writes the answers file.** In the intent drain it applies, for one task
   and one pass, only the newest `answers` intent that has `submit` set, or the newest overall
   when none has; the others are deleted unapplied and logged. It writes the answers file of
   the open request's stage atomically into the worktree, with `actor` from the intent and
   `submitted_at` from the intent's creation time. It drops an intent, with the usual
   `intent-dropped` event and its reason, when: the task has no open request; the task is
   not waiting for the operator (parked for input or review, or at the gate before the grace
   park); the intent's `revision` is not the open request's; or the intent is a draft and the
   file on disk is already submitted. It fails the intent, deleted and logged, when `.agent/`
   in the worktree is not a directory. The `revision` of a `plan-approval` request is its
   fingerprint; an `answers` request gets a SHA-256 of the page bytes when it is armed.
9. **A submission resumes the session; a draft does not.** `"changes"` wakes the parked or
   waiting session with the message "Answers in `<file>` (changes). Apply them as feedback."
   `"approve"` wakes it with "Answers in `<file>` (approve). Approved." A draft writes the
   file and wakes nothing. The resumed session treats the file as the operator's reply under
   the rules of its prompt: feedback is never an approval; only `"approve"` ends the gate.
10. **The answers files are review artifacts.** The dispatcher registers
    `.agent/review-answers.json` as `review-answers` and `.agent/questionnaire-answers.json` as
    `questionnaire-answers` automatically when they exist, takes their snapshot like any
    registered file, and lists them on the task page.
11. **Review artifacts of a terminal task expire after 7 days**, instead of 30.
12. **A text answer writes the same file.** When the operator answers in the terminal pane or
    by the reply route, the session writes the answers file itself, in the schema, with
    `actor` `"text"`, `submitted` `"changes"` or `"approve"` as the text says, and the answers
    it could read from the text. The file is the one record a later session reads.
13. **Later sessions read the file.** The implement session and the review session read
    `.agent/review-answers.json` when it exists. A session that finds a question id it cannot
    map to its page or spec ignores that answer and lists it under the Corrections section of
    the next review page it writes, or in its report when it writes no page.
14. **The skill is one skill, pinned.** `review-page` lives in `jesdi/general-skills` with
    `SKILL.md`, `template.html` and `schema.md`. `.my-skills.json` of agent-ops pins it at the
    released package version, and the agent-ops tests use a fixture copy of the template, so
    the tests pass before the release exists. `jesdi/portfolio_eval` pins it too before the box
    runs a plan stage with the new prompt.
15. **The prototype is the reference for the look.** The template renders the layout of the
    accepted prototype (artifact `4R6Ryt3LhQpWRYdREa4Mp8`): tickets, boxed questions with a
    recommended chip and a note field, corrections, track pills, a fixed bottom bar with "Send
    changes" and "Approve" where "Approve" needs a second tap; in questionnaire mode the
    tickets, corrections and track sections are absent and the bar has one button, "Send
    answers". It works at 400 px width without a horizontal scroll, in light and dark theme,
    with the console's colors.

## Scenarios

### Scenario: the plan session signals the gate with the page

- **Given** the plan session finished stage 2 with three open questions
- **When** it writes `.agent/review.html` from the template, registers it, and writes
  `{"stage": "plan", "status": "awaiting-review", "artifact": ".agent/review.html", "open_questions": 3, "note": "6 tickets, 3 open questions"}`
- **Then** the dispatcher moves the task to `awaiting-plan-review` with a `plan-approval`
  request whose path is `.agent/review.html`, and the console's request route returns the
  page's HTML with media type `text/html`.

### Scenario: a page without the template marker is bounced

- **Given** the plan session signals `awaiting-review` with `.agent/review.html` that is
  12 KiB of HTML without the template marker
- **When** the dispatcher reads the signal
- **Then** the task does not enter the gate, the session is told "review page lacks the
  template marker", and the retry count of the plan stage goes up by one, as for a bad summary.

### Scenario: a page over the size limit is bounced

- **Given** `.agent/review.html` is 300 KiB and carries the marker
- **When** the dispatcher reads the `awaiting-review` signal
- **Then** the task does not enter the gate and the session is told the page exceeds 256 KiB.

### Scenario: the spec session asks with the page

- **Given** the spec session has two open decisions and no `spec-ready` label
- **When** it writes `.agent/questionnaire.html` in questionnaire mode and signals
  `awaiting-answers` with that path
- **Then** the task parks with an `answers` request whose path is `.agent/questionnaire.html`,
  the request route returns the page, and no `.agent/questionnaire.md` exists.

### Scenario: the review route shows only the page

- **Given** task #412 is at the gate
- **When** the operator opens `/task/portfolio_eval/412/review` on a phone 400 px wide
- **Then** the console shows the page in a sandboxed iframe that fills the viewport, with no
  task header, no history and no artifacts panel, and the page itself scrolls; no horizontal
  scroll appears.

### Scenario: the task page links to the review route

- **Given** task #412 is at the gate
- **When** the operator opens `/task/portfolio_eval/412`
- **Then** the request panel shows "plan awaiting review" with a link "open review" to
  `/task/portfolio_eval/412/review`, and no "approve plan" button.

### Scenario: a message from another window is ignored

- **Given** the review route is open and the bridge listens
- **When** a script in the console's own window posts
  `{"type": "answers", "v": 1, "answers": {"format": "a"}, "submit": "approve"}` to the window
- **Then** no request reaches the answers route and no intent is written.

### Scenario: a message with an unknown version is ignored

- **Given** the review route is open
- **When** the iframe posts `{"type": "answers", "v": 2, "answers": {"format": "a"}, "submit": null}`
- **Then** no request reaches the answers route and the route shows "this review page needs a
  newer console".

### Scenario: saved answers return on another device

- **Given** the snapshot of `.agent/review-answers.json` holds
  `{"format": "b", "track": "standard"}` from the operator's laptop
- **When** the operator opens the review route on the phone and the page posts `ready`
- **Then** the console posts `{"type": "restore", "v": 1, "answers": {"format": "b", "track": "standard"}}`
  to the iframe, and the page shows option `b` of `format` selected and `standard` chosen.

### Scenario: no saved answers yet

- **Given** no answers file exists for the task
- **When** the page posts `ready`
- **Then** the console posts `restore` with `"answers": {}`.

### Scenario: quick selections become one intent

- **Given** the review route is open
- **When** the operator picks `format` `a`, then `limit` `b`, then `headers` `a` within one
  second
- **Then** the console posts one request to `/api/task/portfolio_eval/412/answers` with
  `{"answers": {"format": "a", "limit": "b", "headers": "a", "track": "standard"}, "submit": null}`,
  the route returns 202, and one intent file with action `answers` exists.

### Scenario: a button press is posted at once

- **Given** the operator picked `format` `a` 200 ms ago and the debounce is pending
- **When** they press "Send changes"
- **Then** the console posts one request at once with `"submit": "changes"` and the pending
  draft is not posted separately.

### Scenario: an oversized answers body is refused

- **Given** the review route is open
- **When** a request reaches the answers route whose JSON is 70 KiB
- **Then** the route answers 422 and writes no intent.

### Scenario: a nested answers object is refused

- **When** a request reaches the answers route with `{"answers": {"format": {"x": 1}}, "submit": null}`
- **Then** the route answers 422 and writes no intent.

### Scenario: the dispatcher writes the draft file

- **Given** an `answers` intent for #412 with `{"answers": {"format": "a"}, "submit": null}`
  from actor `jesdi`, and #412 at the gate
- **When** the dispatcher drains intents
- **Then** `/srv/wt/portfolio_eval/412/.agent/review-answers.json` holds
  `{"v": 1, "stage": "plan", "submitted": null, "submitted_at": null, "actor": "jesdi", "answers": {"format": "a"}}`,
  the intent is deleted, an `intent-applied` event is logged, and no session is resumed.

### Scenario: two drafts in one pass, the newest wins

- **Given** two `answers` intents for #412 created at 10:00:01 with `{"format": "a"}` and at
  10:00:04 with `{"format": "b"}`, both with `submit` null
- **When** the dispatcher drains intents
- **Then** the answers file holds `{"format": "b"}`, both intents are deleted, and the file was
  written once.

### Scenario: a submission beats a later draft in the same pass

- **Given** an `answers` intent at 10:00:01 with `submit` `"changes"` and one at 10:00:03 with
  `submit` null, both for #412
- **When** the dispatcher drains intents
- **Then** the file holds the answers of the 10:00:01 intent with `"submitted": "changes"`,
  and the session is resumed once.

### Scenario: an approval on a stale page is dropped

- **Given** #412 was at the gate with revision `r1`, the operator sent "changes", the plan
  session rewrote the page and the task is at the gate again with revision `r2`
- **When** an `answers` intent with `submit` `"approve"` and `revision` `r1` is drained
- **Then** no file is written, the session is not resumed, and an `intent-dropped` event says
  "stale revision".

### Scenario: the console reloads a changed page

- **Given** the review route is open on revision `r1`
- **When** the request route starts returning revision `r2`
- **Then** the console reloads the iframe with the new page, posts `restore` with the saved
  answers after the new `ready`, and shows "the plan changed; your selections were reset to
  the saved ones".

### Scenario: an approval while the session reworks is dropped

- **Given** the plan session was resumed with "changes" at 10:00 and reports `working`
- **When** an `answers` intent with `submit` `"approve"` is drained at 10:01
- **Then** no file is written, the session gets no message, and an `intent-dropped` event
  says "no open request" (the rework cleared the request).

### Scenario: a draft never overwrites a submission

- **Given** `.agent/review-answers.json` holds `"submitted": "approve"` written by the session
  from a terminal answer at 10:00:05
- **When** a draft intent created at 09:59:50 is drained at 10:00:10
- **Then** the file is unchanged and an `intent-dropped` event says "already submitted".

### Scenario: a pending intent is part of restore

- **Given** the answers file holds `{"format": "b"}` and a pending `answers` intent, not yet
  drained, holds `{"format": "b", "limit": "a"}`
- **When** the phone opens the review route and the page posts `ready`
- **Then** `restore` carries `{"format": "b", "limit": "a"}`.

### Scenario: the worktree is gone when the intent is drained

- **Given** an `answers` intent for #412 and a task record whose worktree directory no longer
  exists
- **When** the dispatcher drains it
- **Then** nothing is created on disk, the intent is deleted, and the failure is logged.

### Scenario: an intent for a task with no request is dropped

- **Given** #412 is in the implement stage with no open request
- **When** an `answers` intent for #412 is drained
- **Then** no file is written, the intent is deleted, and an `intent-dropped` event names the
  reason "no open request".

### Scenario: "Send changes" resumes the plan session as feedback

- **Given** #412 is parked at the gate and an `answers` intent arrives with `submit` `"changes"`
- **When** the dispatcher drains it
- **Then** the file holds `"submitted": "changes"`, the plan session is resumed with the
  message "Answers in .agent/review-answers.json (changes). Apply them as feedback.", and the
  task is not approved: it returns to `awaiting-plan-review` when the session reports again.

### Scenario: "Approve" resumes the plan session as approval

- **Given** #412 is parked at the gate and an `answers` intent arrives with `submit` `"approve"`
  and `"track": "security"`
- **When** the dispatcher drains it
- **Then** the plan session is resumed with "Answers in .agent/review-answers.json (approve).
  Approved.", and when it signals `done` with `"track": "security"`, implement starts on the
  `security` track.

### Scenario: the answers file is listed as an artifact

- **Given** `.agent/review-answers.json` exists in the worktree
- **When** the dispatcher collects artifacts
- **Then** the index lists `review-answers` "Review answers" with that path, with a snapshot,
  and the task page shows it in the artifacts panel.

### Scenario: review artifacts expire after 7 days

- **Given** #412 became terminal on 2026-10-20 10:00 UTC with a review page and an answers
  file in the index
- **When** cleanup runs on 2026-10-27 10:01 UTC
- **Then** the snapshot content is deleted and the artifacts show as expired; on 2026-10-27
  09:59 UTC they are still there.

### Scenario: a text answer writes the file

- **Given** #412 is at the gate and the operator types in the terminal pane "format b, limit a,
  approved"
- **When** the plan session handles the message
- **Then** it writes `.agent/review-answers.json` with `"actor": "text"`,
  `"submitted": "approve"`, `"answers": {"format": "b", "limit": "a"}`, and then signals `done`.

### Scenario: an answer a later session cannot map

- **Given** `.agent/review-answers.json` holds `{"format": "b", "pagination": "a"}` and the
  current review page has no question `pagination`
- **When** the plan session applies the feedback and writes the next review page
- **Then** `format` `b` is applied, and the Corrections section of the new page lists
  "answer `pagination` = `a` ignored: no such question".

### Scenario: the implement session reads the answers

- **Given** `.agent/review-answers.json` holds `{"format": "b"}` with `"submitted": "approve"`
- **When** the implement session starts
- **Then** its prompt names the file as the operator's decisions and the session reads it
  before the tickets.

### Scenario: the questionnaire page in the viewer

- **Given** the spec session parked with `.agent/questionnaire.html`
- **When** the operator opens the review route
- **Then** the page shows the questions, no tickets, no corrections, no track pills, and one
  button "Send answers"; pressing it posts `"submit": "changes"`.

### Scenario: the page at phone width in dark theme

- **Given** the review page of #412
- **When** it renders at 400 px width with the dark theme
- **Then** no element overflows the width, the bottom bar stays above the safe area, the
  selected option is distinguishable from the others, and the colors are the console's dark
  tokens.

### Scenario: the template is missing on the box

- **Given** the `review-page` skill is not installed in the task worktree
- **When** the plan session reaches step 5
- **Then** it signals `blocked` with the note "review-page skill not installed", and the task
  parks with that reason.

## Out of scope

- The Telegram notification and its link stay as they are.
- The request panel does not resize to the page; the review route replaces that need.
- The answers file holds no option texts.
- Validation of question ids by the dispatcher.
- Issue #161 (Codex subagents on the box) and the packaging of `to-questionnaire`.
