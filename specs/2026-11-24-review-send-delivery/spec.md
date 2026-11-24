# Review page answers that always reach the session — Spec

Issue #281. The decisions below were settled with the operator in the
issue thread (grilling session) and are carried over as they were made:

> 1. The Artifacts panel `plan-review` link opens the console `/review` route,
>    which has the Send bar, instead of the raw HTML.
> 2. A raw-file view (herdr file viewer, the file opened directly) shows a
>    banner linking to the task's console review page and disables its inputs.
>    At park, the dispatcher writes
>    `<meta name="agent-ops-console" content="<console_url>/task/<repo>/<n>/review">`
>    from the existing `cfg.console_url`. With no URL configured, the banner is
>    plain text. The page keeps its no-network rule.
> 3. Delivery status: `GET …/request` returns
>    `delivery: {state: queued|delivered|dropped, reason}`, worked out from the
>    pending intent, the latest `intent-dropped` event after the submit, and the
>    answers file. Nothing new is stored. The Send bar shows it, keeps the
>    answers on a drop, and offers Resend.
> 4. Answers submitted while the session is busy are held until it parks
>    instead of being dropped. If the page revision changed meanwhile, they are
>    dropped, the console shows "page changed, check and resend", and answers
>    are restored into the new page where the question ids still match.
> 5. Verify end to end on a phone that the Send bar is visible on mobile
>    `/review`.
>
> Out of scope: lavish as the gate surface.

## What the code says underneath the decisions

- **Artifact ids are not fixed.** The plan prompt registers the review page
  as `plan-review`, but the spec prompt lets the session pick the id of the
  questionnaire, and the bug is the same for both. The link is therefore
  chosen by **path**: an artifact whose `path` equals the open request's page
  path (`content.path` of `GET …/request`) links to the review route. With no
  open request on that path, the review route would only say "no open
  request", so the link stays the raw preview (which then shows the banner of
  requirement 2).
- **The page revision is the hash of the page's bytes**
  (`artifacts.page_revision`, and `plan_revision` includes the page). Writing
  the meta changes the bytes, so it is written before the request's
  fingerprint is taken, and rewriting an existing `agent-ops-console` meta
  never adds a second one. Otherwise every injection would make the console's
  revision stale. The request is first armed at `HELD_PLAN_APPROVAL` for a
  plan, before the grace park, so "at park" means "when the request is
  armed" (held or parked).
- **The banner and the disabled inputs need the template's script.** The page
  is the `review-page` skill's template, pinned in `.my-skills.json`
  (`review-page` 0.1.0, package 0.0.30) and mirrored in
  `tests/fixtures/review-page.html`; sessions fill content slots only. The
  template's script already tells the raw view apart
  (`embedded = window.parent !== window`). The banner logic ships in a new
  release of that skill package, which lives in another repository
  (`jesdi/general-skills`, installed by `scripts/install-skills.sh`); this repo bumps the pin and the fixture
  (`tests/test_sessions_share_one_record_acceptance.py` keeps them equal).
- **"Session busy" is rarely what a busy session gets today.** A request
  exists only while the status is held, parked for review or answers, or
  WAKING from one of those (`state._request`). Once the session runs, there is
  no request, so `drop_reason` answers "no open request", not "session busy".
  Holding therefore covers WAKING and RUNNING: the intent is kept, not
  deleted, until the task waits for the operator again or ends.
- **Dropped events do not tell a submission from a draft, nor answers from
  other actions.** `intent-dropped` carries repository, issue, actor and the
  reason in `detail`. A stale draft from an old tab must not show as a
  dropped submission, so the event of a dropped answers intent must say that
  it was an answers submission. That is a field of a record that is already
  written, not new state.
- **After delivery the request closes.** A delivered submission wakes the
  session, the status leaves the request, and `GET …/request` returns `null`
  today. The delivered state must still reach the route that sent it. The
  delivery is therefore read where it does not depend on an open request
  (see `design.md`).
- **Mobile layout is already covered.** `frontend/e2e/review-route.spec.ts`
  checks the bar at 400 px; the `pixel-7` Playwright project runs every e2e
  test on a phone viewport. Requirement 5 adds the operator's check on a real
  phone.

## Requirements

1. **The artifact link of the open request's page opens the review route.**
   In the Artifacts panel, an artifact whose `path` equals the `content.path`
   of the task's open request links to `/task/<repo>/<n>/review` in the same
   tab, for both request kinds (`plan-approval`, `answers`). Every other
   artifact keeps its current link. Scenarios 1, 2.
2. **The dispatcher stamps the console link into the page when it arms the
   request.** With `console_url` set, the page holds exactly one
   `<meta name="agent-ops-console" content="<console_url>/task/<repo>/<n>/review">`
   (repo and issue URL-encoded, the value HTML-escaped) before the request's
   revision is computed, so the console's revision matches the bytes on disk.
   With `console_url` empty, no such meta is written. A page that cannot be
   written (not a regular file, outside the worktree) is left as it is and
   the request is armed as before. Scenarios 3, 4, 5.
3. **The raw-file view sends the operator to the console.** A review page
   opened outside the console's iframe shows a banner at the top. With the
   meta, the banner is a link to that URL; without it, plain text saying
   the answers are sent from the console's review page. Every input of the
   page is disabled. Inside the console's iframe there is no banner and the
   inputs work. The page makes no network request either way. Scenarios 6, 7.
4. **The console reads the delivery of the task's latest submission.**
   The console's API reports `delivery: {state, reason, submit, revision}`
   with `state` one of `queued`, `delivered`, `dropped`, or `delivery: null`
   when the task has no submission. `revision` is the page revision the
   submission was made on, `submit` its kind. It is worked out from what
   exists: a pending submission intent of the task is `queued`; otherwise
   the newer, by submission time, of (a) the answers file's submission and
   (b) the latest `intent-dropped` event of an answers submission of the
   task decides `delivered` or `dropped` (with the event's reason). Drafts
   never set a delivery state. Nothing new is stored beyond marking the
   dropped event as an answers submission with its submit, revision and
   submission time. The review route shows a delivery only for the revision
   it shows or the revision this tab sent on, so a drop of an earlier round
   never shows on a new round's page. Scenarios 8, 9, 10, 11.
5. **The delivered state reaches the route after the request closes.** When
   the submission woke the session and the request closed, the review route
   shows "delivered" rather than "no open request". Scenario 12.
6. **The Send bar shows the delivery state.** After a press it shows
   "queued" until the state changes, then "delivered" or "dropped: <reason>".
   On a drop it keeps the answers on the page and shows a **Resend** button
   that submits them again with the same submit kind on the current
   revision. "Sent" is no longer shown on the `202` alone. Scenarios 13, 14.
7. **A submission made while the session is busy is held.** While the task
   is WAKING or RUNNING, an answers submission intent stays pending across
   passes instead of being dropped. When the task waits for the operator
   with a request at the intent's revision, it is applied as today. When
   the request's revision differs, it is dropped as "stale revision". When
   the task ends (done, failed, canceled) or parks with no request, it is
   dropped as "no open request". A task held at a login prompt
   (HELD_LOGIN) is a live session too: the submission is held. Drafts keep
   today's rule (dropped).
   Scenarios 15, 16, 17.
8. **A changed page restores what still fits.** When a submission is dropped
   as "stale revision" and the request now shows another revision, the
   review route shows "page changed, check and resend" and restores the
   dropped answers into the new page for the question ids the new page still
   has. Ids the new page lacks are dropped silently. Scenario 18.
9. **The Send bar is visible on a phone.** On `/review` at a phone viewport
   the Send control is inside the viewport without scrolling, and the
   operator confirms it on a real phone (PR evidence). Scenario 19.

## Scenarios

### Scenario 1: the reported bug — review page artifact opens without Send (requirement 1)

- **Given** task `widget#42` parked for plan review with request
  `{kind: plan-approval, revision: r1, content.path: .agent/review.html}`
  and an artifact `{id: review, name: "Review page", path: .agent/review.html,
  status: local, url: /api/task/widget/42/artifacts/review}`
- **When** the operator opens `/task/widget/42` and taps "Open Review page"
  in the Artifacts panel
- **Then** the link's `href` is `/task/widget/42/review` (today it is
  `/api/task/widget/42/artifacts/review`, the raw page with no Send
  control), and the opened page shows the "Send changes" button.
- The same holds for `{kind: answers, content.path: .agent/questionnaire.html}`
  with the "Send answers" button. Reproduced by the committed tests in
  `frontend/src/components/__tests__/ArtifactsPanel.test.tsx` and
  `frontend/e2e/artifacts-flow.spec.ts`.

### Scenario 2: other artifacts keep their links (requirement 1)

- **Given** the request of scenario 1 and an artifact `{id: prototype,
  path: .agent/prototype.html, url: /api/task/widget/42/artifacts/prototype}`
- **When** the Artifacts panel renders
- **Then** "Open Prototype" links to `/api/task/widget/42/artifacts/prototype`
  in a new tab.
- **Given** no open request and the artifact `{id: review, path: .agent/review.html}`
- **Then** "Open Review page" links to `/api/task/widget/42/artifacts/review`.

### Scenario 3: the meta is written before the revision (requirement 2)

- **Given** `console_url: https://ops.example` and a spec session that
  signals `awaiting-answers` with `.agent/questionnaire.html` for `widget#42`
- **When** the dispatcher parks the task
- **Then** the page holds exactly one
  `<meta name="agent-ops-console" content="https://ops.example/task/widget/42/review">`,
  the request's revision equals `page_revision` of the page as it is now on
  disk, and `GET /api/task/widget/42/request` returns that revision.
- **Given** the same for a plan session's `.agent/review.html` held at the
  gate, **then** `plan_revision` is computed over the page with the meta.

### Scenario 4: re-arming never duplicates the meta (requirement 2)

- **Given** a page that already holds
  `<meta name="agent-ops-console" content="https://old.example/task/widget/42/review">`
- **When** the request is armed again with `console_url: https://ops.example`
- **Then** the page holds one `agent-ops-console` meta, with the new URL.
- **Given** a page that already holds the meta with the current URL
- **When** the next pass reads the report again
- **Then** the page bytes and the request's revision are unchanged.

### Scenario 5: no console URL, or an unwritable page (requirement 2)

- **Given** `console_url` is empty
- **When** the request is armed
- **Then** the page bytes are unchanged and the request is armed.
- **Given** `.agent/questionnaire.html` is a symlink
- **Then** the dispatcher writes nothing through it and arms the request as
  it does today (the shape check decides).
- **Given** a page without the `agent-ops-review` marker (it fails the shape
  check)
- **Then** the dispatcher writes no meta into it.

### Scenario 6: the raw view shows the banner and disables inputs (requirement 3)

- **Given** a page from the released template with the meta of scenario 3
- **When** it is opened directly (not in an iframe)
- **Then** a banner at the top links to
  `https://ops.example/task/widget/42/review`, every `input`, `textarea` and
  `select` is disabled, and the page made no network request.
- **Given** the page without the meta, **then** the banner is plain text
  with no link and the inputs are disabled.

### Scenario 7: the console's iframe shows no banner (requirement 3)

- **Given** the same page served on `/task/widget/42/review`
- **When** it loads in the sandboxed iframe
- **Then** no banner shows, the operator can select an option, and the
  selection posts a draft as today.

### Scenario 8: queued (requirement 4)

- **Given** a pending `answers` intent `{submit: changes, revision: r1}` for
  `widget#42`
- **When** the console reads the delivery of `widget#42`
- **Then** `delivery` is `{state: queued, reason: "", submit: changes,
  revision: r1}`.

### Scenario 9: delivered (requirement 4)

- **Given** no pending intent and `.agent/review-answers.json` with
  `{submitted: changes, revision: r1, submitted_at: 10:00}` and no answers
  drop event after 10:00
- **Then** `delivery` is `{state: delivered, reason: "", submit: changes,
  revision: r1}`.

### Scenario 10: dropped (requirement 4)

- **Given** no pending intent, no submitted answers file, and an
  `intent-dropped` event for an answers submission `{submit: changes,
  revision: r1}` of `widget#42` made at 10:01, with reason "stale revision"
- **Then** `delivery` is `{state: dropped, reason: "stale revision",
  submit: changes, revision: r1}`.
- **Given** additionally an answers file submitted at 10:02, **then** the
  state is `delivered` (the newer submission wins).
- **Given** instead an answers file submitted at 10:02 and a drop event,
  written at 10:03, of a submission made at 10:00, **then** the state is
  `delivered`: submission time decides, not the time of the event.

### Scenario 11: a stale draft is no dropped delivery (requirement 4)

- **Given** only an `intent-dropped` event for an answers **draft** (submit
  null) of `widget#42`, reason "stale revision"
- **Then** `delivery` is `null`.
- **Given** a drop event of a submission on r0 (an earlier round) and the
  route now showing r1, with no submission from this tab
- **Then** the Send bar shows no delivery state.

### Scenario 12: delivered after the request closed (requirement 5)

- **Given** the operator pressed "Send answers" on `/task/widget/42/review`
  and the drain applied it, the session woke, and `GET …/request` no longer
  has an open request
- **Then** the route shows "delivered", not "no open request".

### Scenario 13: the Send bar follows the state (requirement 6)

- **Given** the route on revision r1
- **When** the operator presses "Send answers" and the API answers 202
- **Then** the bar shows "queued", and "delivered" once `delivery.state` is
  `delivered`; it never shows "sent" from the 202 alone, and no Resend
  button shows while the state is `queued` or `delivered`.

### Scenario 14: Resend after a drop (requirement 6)

- **Given** the operator sent `{format: b}` with "Send changes" and
  `delivery` becomes `{state: dropped, reason: "no open request"}`
- **Then** the bar shows "dropped: no open request", the page still shows
  `format: b`, and a Resend button is visible.
- **When** the operator presses Resend
- **Then** one POST `{answers: {format: b}, submit: changes, revision: <current>}`
  is made.

### Scenario 15: held while busy, then applied (requirement 7)

- **Given** `widget#42` RUNNING and a pending answers intent
  `{submit: changes, revision: r1}`
- **When** a dispatcher pass runs
- **Then** the intent is still pending and no `intent-dropped` event is
  written.
- **When** the task later parks with an answers request at revision r1
- **Then** the next pass writes `.agent/questionnaire-answers.json` with
  `submitted: changes` and wakes the session.

### Scenario 16: held, then the page changed (requirement 7)

- **Given** the held intent of scenario 15
- **When** the task parks with a request at revision r2
- **Then** the intent is dropped with "stale revision" and the answers file
  is not written.

### Scenario 17: held, then the task ended (requirement 7)

- **Given** the held intent of scenario 15
- **When** the task becomes done (or failed, canceled, or parked-human with
  no request)
- **Then** the intent is dropped with "no open request".
- **Given** a draft intent `{submit: null, revision: r1}` while RUNNING,
  **then** it is dropped on that pass as today.

### Scenario 18: page changed, answers restored where ids match (requirement 8)

- **Given** the operator sent `{format: b, scope: wide, "format.note": "x"}`
  on r1 and it was dropped as "stale revision"; the request is now r2, whose
  page has question `format` but not `scope`
- **When** the route shows r2
- **Then** it shows "page changed, check and resend", the new page has
  `format: b` and the note `x`, `scope` is not restored, and nothing is
  submitted until the operator presses Send.

### Scenario 19: Send bar on a phone (requirement 9)

- **Given** `/task/widget/42/review` with an answers request at a
  390×844 viewport (the `pixel-7` project)
- **Then** the "Send answers" button is fully inside the viewport, at least
  44 px tall, with no page scroll.
- The PR's evidence records the operator's check on a real phone.

## Out of scope

- lavish as the gate surface (needs a live agent blocked on `poll`, which
  breaks parking, an unauthenticated port and an npx fetch at run time).
- Any submit control inside the session-written page or the raw view.
- Any network call from the review page.
- Restoring dropped answers on another device: the restore of requirement 8
  is the console tab that sent them.
- Holding drafts while the session is busy.
