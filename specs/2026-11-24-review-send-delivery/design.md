# Review page answers that always reach the session — Design

## Decisions

- **The artifact link is chosen by path, on the client.** The Artifacts panel
  reads the task's request (the query the task page already has) and links
  the artifact whose `path` equals `content.path` (both normalized:
  `./` and duplicate slashes removed) to `/task/<repo>/<n>/review`, same tab.
  — Costs one more read of an already cached query; no API change.
- **Delivery has its own read: `GET /api/task/<repo>/<n>/delivery`.**
  `GET …/request` returns `null` once the request closes (after a delivery,
  or while the session runs), so it cannot carry the delivered or held
  state. The request route stays as it is. — Deviates from the operator's
  wording "`GET …/request` returns delivery" (open question on the review
  page); the alternative leaves a held submission shown as delivered.
- **Delivery is the task's latest submission, with its revision.** The
  server does not decide which round a delivery belongs to (it stores no
  arming time); it reports `{state, reason, submit, revision}` and the
  review route shows it only for the revision it shows, or the revision
  this tab last sent on. — A tab that reloads forgets what it sent; an old
  round's drop never shows on a new page.
- **Submission time decides between the answers file and a drop event.**
  The drop event records the dropped intent's `created_at`; the answers file
  already records it as `submitted_at`. — Compares two times written from
  the same field instead of two clocks.
- **The drop event of an answers submission carries `submission: {submit,
  revision, created_at}`.** Drafts and other actions write no such field.
  — One optional key on an existing record; old lines simply lack it.
- **Delivery reads the current event log file only.** — ponytail: a drop
  rotated into an archive (5 MB) is forgotten; the state falls back to the
  answers file or `null`.
- **Held = the session is live:** WAKING, RUNNING, HELD_LOGIN. The drain
  skips a held answers submission: not applied, not deleted, no event.
  Every other status goes through today's `drop_reason` (a park without a
  request or an ended task gives "no open request"). — An intent can wait
  as long as the session runs; the console shows it queued meanwhile.
- **The meta is stamped where the revision is read.** The dispatcher writes
  the `agent-ops-console` meta in the reads of an `awaiting-review` or
  `awaiting-answers` report, before `plan_revision` / `page_revision`, only
  into a page that passes the shape check, right after the
  `agent-ops-review` marker, and only when the bytes would change. — Each
  pass re-reads the report, so the stamp must be idempotent; a changed
  `console_url` mid-park changes the revision and drops an in-flight submit
  as "stale revision", which the console then reports truthfully.
- **The write goes through `write_worktree_file` with the page's mode kept.**
  — Never through a symlink; the shape check already refused one.
- **Restore after a page change reuses the template's `restore`.** It sets
  only the inputs the new page has, so the console sends the whole dropped
  set and ids the page lacks fall away. — No id list on the console side.
- **Resend reposts the tab's own last submission** (answers and submit) on
  the current revision. — Nothing is read back from the server, which no
  longer has the dropped answers.
- **The banner ships in `jesdi/general-skills`; this repo bumps the pin and
  the fixture.** — The last ticket waits on a release outside this repo
  (open question on the review page).

## Data model

No table. The records touched:

| Record | Field | Constraint | Why |
|---|---|---|---|
| `events.jsonl` `intent-dropped` line | `submission: {submit, revision, created_at}` | present only for an `answers` intent whose `submit` is `changes` or `approve` | tells a dropped submission from a draft and from other actions, and binds it to a revision and a submission time |
| intent file (`intents/*.json`) of an answers submission | — | survives a pass while the task is WAKING, RUNNING or HELD_LOGIN | requirement 7: held, not dropped |
| review page (`.agent/review.html`, `.agent/questionnaire.html`) | `<meta name="agent-ops-console">` | at most one; written only into a page that passes the shape check; absent when `console_url` is empty | the page revision is the hash of its bytes |
| `delivery` (read only) | `{state: queued\|delivered\|dropped, reason, submit, revision}` or `null` | derived on every read, never stored | "nothing new is stored" |

## Identities

None.

## Seams

- `operator_intents._apply_intents(cfg, deps)` — existing; the drain of one
  pass (`tests/test_answers_intent_acceptance.py`).
- `GET /api/task/<repo>/<n>/delivery` — new.
- A dispatcher pass on a task that reports `awaiting-review` /
  `awaiting-answers`, then `GET /api/task/<repo>/<n>/request` — existing
  (`tests/test_review_page_gate_acceptance.py`).
- The console's `/task/<repo>/<n>/review` route — existing (Vitest with MSW,
  `frontend/e2e/review-route.spec.ts`).
- The Artifacts panel of `/task/<repo>/<n>` — existing
  (`ArtifactsPanel.test.tsx`, `frontend/e2e/artifacts-flow.spec.ts`).
- The review page opened directly, from `tests/fixtures/review-page.html`
  — existing fixture, new e2e check.
