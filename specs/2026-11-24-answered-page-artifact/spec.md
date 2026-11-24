# Answers artifact: the answered page, kept in the box archive — Spec

Issue #282. Root cause and context in `proposal.md`. The reproduction is
`tests/test_answered_page_artifact_acceptance.py` (committed first, failing).

Terms: an **answers kind** is one of the two operator request kinds of
`dispatcher.answers.ANSWERS_FILES` — `plan-approval` (file
`.agent/review-answers.json`, artifact id `review-answers`, name "Review
answers") and `answers` (file `.agent/questionnaire-answers.json`, artifact id
`questionnaire-answers`, name "Questionnaire answers"). The **request page** is
the page the open request names (`operator_request(task).path`, in practice
`.agent/review.html` or `.agent/questionnaire.html`). The **answered page** is
the request page with a set of answers filled in and its inputs read-only. The
**archive** is `<state_dir>/artifacts/<key>/` (`state.archive_root`), whose
`content/<id>` the console serves.

## Requirements

1. **The answers artifact is the answered page.** The content stored under
   `review-answers` / `questionnaire-answers` is an HTML document with media type
   `text/html`: the request page's markup, with the submitted answers filled in.
   For every answer key `q` with value `v`, the radio input named `q` whose value
   is `v` is checked; for every key `q.note`, the textarea `note-q` holds that
   text. Opening the artifact link (`GET /api/task/{repository}/{issue}/artifacts/{id}`)
   returns it inline under the existing sandbox CSP, never as JSON and never as a
   download.
2. **Read-only.** In the answered page every `input`, `textarea`, `select` and
   `button` is disabled, and no change can be made or posted from it, whether it
   is opened in a tab or embedded.
3. **Nothing submitted is hidden.** An answer whose key matches no input of the
   page (a renamed question, a session's text answer) is still shown in the
   answered page, as its key and value in plain text.
4. **Answer text is text.** Answer values and notes are inserted as text: markup
   in them (`<`, `&`, `</textarea>`, `<script>`) shows literally and adds no
   element to the page.
5. **Captured when the submission is recorded.** When the dispatcher records a
   submitted answers intent (`answers.record` with `submit` `changes` or
   `approve`), it stores the answered page in the archive in the same step,
   built from the request page as it is on disk at that moment (the revision the
   answers were made on) and the answers just written. The artifact entry in the
   archive index has the kind's id and name, the answers file's path, the task's
   stage and the capture time. No later read of the worktree is needed for it.
6. **A session's text answer is captured too.** When the collector finds an
   answers file with `submitted` set that it has not captured yet (in practice a
   session wrote it from a text answer: `actor` `"text"`, no revision), it
   stores the answered page from that file and the request page in the
   worktree; with no open request, from the kind's default page
   (`.agent/review.html` for `plan-approval`, `.agent/questionnaire.html` for
   `answers`).
7. **Drafts are not captured.** A draft (`submit` null) never creates or changes
   the answers artifact.
8. **The capture survives.** Once stored, the answered page keeps being served
   unchanged after `answers.discard_stale` removes the answers file, after the
   session rewrites the request page for the next round, and after the worktree
   is removed or broken. A later collection never replaces it with the raw JSON
   file, nor rebuilds it with a newer page for the same answers. Only the next
   captured submission of the same kind replaces it, under the same id and URL.
9. **Capture never blocks the answers.** When the answered page cannot be built
   or stored (request page missing or unreadable, archive write fails), the
   answers file is still written and the session still woken exactly as today;
   the failure is logged as a warning and the previous answers artifact, if any,
   is left as it was.
10. **Retention is unchanged.** The answers artifacts expire with the task's
    other artifacts, seven days after the task ends.

## Scenarios

### Scenario: reproduction — the questionnaire answers open as the answered page

- **Given** task `portfolio_eval#412` at stage `spec`, parked on
  `.agent/questionnaire.html` with a question block `data-q="format"` titled
  "Which export format?", radios `csv` and `parquet`, and textarea `note-format`
- **When** the operator sends answers `{"format": "parquet", "format.note": "keep it columnar"}`
  with `submit: "changes"` on the page's revision, one pass drains it, and the
  collector runs
- **Then** `GET /api/task/portfolio_eval/412/artifacts/questionnaire-answers`
  returns 200 with `Content-Type: text/html`, the body is not JSON, it contains
  "Which export format?", the `parquet` radio is checked, and the `note-format`
  textarea holds "keep it columnar"
- (Today: 200 with `application/json` and the raw answers file.)

### Scenario: reproduction — the worktree is lost before the collector runs

- **Given** the submission of the first scenario has just been drained
- **When** the task worktree is deleted, then the collector runs
- **Then** the same `GET` returns 200 with the answered page of the first scenario
- (Today: 404 "artifact not found".)

### Scenario: reproduction — the next round arms before the collector runs

- **Given** the submission of the first scenario has just been drained
- **When** the session rewrites `.agent/questionnaire.html` (now "Which import
  format?"), `answers.discard_stale` arms revision `rev-2` and removes the
  answers file, then the collector runs
- **Then** the `GET` returns 200 with the page as it was answered: it contains
  "Which export format?", not "Which import format?", with `parquet` checked
- (Today: 404 "artifact not found".)

### Scenario: plan review answers open as the answered review page

- **Given** a task at the plan gate on `.agent/review.html` with questions
  `format` (options `a`, `b`) and `track` (options `standard`, `critical`)
- **When** the operator presses "Approve" with `{"format": "b", "track": "critical"}`
- **Then** `review-answers` opens as `text/html` with `b` and `critical`
  checked, and the listing shows it with status `local` and media type `text/html`

### Scenario: every input of the answered page is disabled

- **Given** the answered page of the first scenario
- **Then** every `input`, `textarea`, `select` and `button` element in it is
  disabled, including the unchosen `csv` radio

### Scenario: an answer with no matching input is shown as text

- **Given** answers `{"format": "parquet", "legacy": "yes"}` on a page with no
  `legacy` input
- **When** the answered page is captured
- **Then** it shows `legacy` and `yes` as plain text, and `parquet` is checked

### Scenario: markup in a note is shown literally

- **Given** a note `"</textarea><script>alert(1)</script> & more"`
- **When** the answered page is captured
- **Then** the page shows that exact text in `note-format`, and the page holds no
  `script` element that was not in the request page

### Scenario: a draft is not captured

- **Given** a parked questionnaire with no answers artifact yet
- **When** the operator's draft `{"format": "csv"}` (`submit: null`) is drained
  and the collector runs
- **Then** the listing has no `questionnaire-answers` entry

### Scenario: a draft after a submission leaves the captured page alone

- **Given** the answered page of the first scenario is captured and the next
  round's page is armed
- **When** a draft `{"format": "csv"}` on the new revision is drained and the
  collector runs
- **Then** `questionnaire-answers` still shows `parquet` checked on the
  "Which export format?" page

### Scenario: the next submission replaces the page under the same URL

- **Given** the answered page of the first scenario is captured, then the next
  round's page "Which import format?" is armed
- **When** the operator submits `{"format": "csv"}` on the new revision
- **Then** the same URL returns the "Which import format?" page with `csv`
  checked, and the listing still has exactly one `questionnaire-answers` entry

### Scenario: a session's text answer is captured by the collector

- **Given** a task parked on `.agent/questionnaire.html` and no answers artifact
- **When** the session writes `.agent/questionnaire-answers.json` itself with
  `"actor": "text"`, `"submitted": "changes"`, no revision and answers
  `{"format": "csv"}`, and the collector runs
- **Then** `questionnaire-answers` opens as the questionnaire with `csv` checked

### Scenario: a later collection does not rebuild the page with the newer page

- **Given** the answered page of the first scenario is captured, and the answers
  file is still in the worktree
- **When** the session rewrites `.agent/questionnaire.html` and the collector runs
- **Then** the artifact content is byte-for-byte what was captured at the drain

### Scenario: the request page cannot be read at capture

- **Given** a parked questionnaire whose request page becomes unreadable between
  the revision check and the capture, and an earlier captured answers artifact
- **When** a submission is drained
- **Then** the answers file is written, the session's wake message is queued as
  today, a warning is logged, and the earlier answers artifact is unchanged

### Scenario: the answered page expires with the task's artifacts

- **Given** a captured `review-answers` artifact on a task that ended at
  2026-10-20T10:00Z
- **When** cleanup runs at 2026-10-27T09:59Z, then at 2026-10-27T10:01Z
- **Then** the content is served at 09:59 and gone (status `expired`) at 10:01

## Existing tests that change

`tests/test_answers_artifacts_acceptance.py` asserts that the automatic
`review-answers` / `questionnaire-answers` content is the raw JSON file and that
a bare JSON file in the worktree is registered. Under requirements 1, 6 and 7
those assertions move to the answered page (and a file without `submitted` is
no longer registered). A session's own manifest entry for an answers file under
another id (its `my-answers` test) is the session's choice and stays a plain
copy of the file.

## Out of scope

- Keeping every round as its own artifact; the id shows the latest submission.
- Drafts as artifacts.
- Converting answers artifacts already archived as JSON.
- #281's Send bar, delivery status and raw-page banner; #280's worktree repair.
- Changes to the review-page template or the answers file format.
