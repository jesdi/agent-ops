# Review page answers that always reach the session

## Why

The operator fills in a review page (`.agent/review.html`, plan mode) or a
questionnaire (`.agent/questionnaire.html`, spec mode) and finds no way to
send it, or sends it and the session never wakes. Diagnosis (#281):

1. **The page is reachable outside the console's review route.** The
   console's Send bar (`SubmitBar` in `frontend/src/pages/ReviewPage.tsx`)
   exists only on `/task/<repo>/<n>/review`, below the sandboxed iframe, by
   design: a button inside the session-written page could press itself.
   But the same page is also registered as a task artifact, and the
   Artifacts panel (`frontend/src/components/ArtifactsPanel.tsx`) links
   every local artifact to its raw preview,
   `/api/task/<repo>/<n>/artifacts/<id>` (`web/artifacts.py` `_preview`). The
   herdr file viewer opens the same raw file. There the page's script sees
   `window.parent === window`, so it posts nothing: the inputs work, the
   answers go nowhere, and there is no Send control. Reproduced by the
   committed tests in `frontend/src/components/__tests__/ArtifactsPanel.test.tsx`
   and `frontend/e2e/artifacts-flow.spec.ts` (the link's `href` is the raw
   artifact URL, not the review route).
2. **"Sent" is shown when the API accepts, not when the session gets it.**
   The console shows "sent" on the `202` of `POST …/answers`. The dispatcher
   may then drop the intent in `answers.drop_reason` ("stale revision",
   "session busy", "already submitted", "no open request"). The drop is only
   logged as an `intent-dropped` event (`dispatcher/operator_intents.py`
   `_apply_intents`), so the operator never learns of it and the session
   never wakes.
3. **A submission made while the session is busy is thrown away.**
   `drop_reason` returns "session busy" whenever the task is not waiting
   for the operator, and the drain deletes the intent, even when the session
   is about to park on the very same page revision.

## For whom

The operator, answering review pages and questionnaires on the console,
mostly on a phone.

## Goal

Wherever the operator can fill in a review page or questionnaire, a visible
Send control submits it, and the console tells the truth about whether the
answers reached the session (queued, delivered, or dropped with a reason and
a way to resend).

## Non-goals

- lavish as the gate surface. It needs a live agent blocked on `poll`,
  which breaks parking, plus an unauthenticated port and an npx fetch at run
  time. It stays a local-only tool.
- A Send control inside the session-written page or the raw-file view: the
  console's buttons stay the only ones that submit.
- Any network call from the review page itself (its no-network rule stays).
- Storing delivery state: it is worked out from what already exists.
- Changes to the `review-page` skill's template script.
