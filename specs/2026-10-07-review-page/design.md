# Review page: one interactive page for the plan gate and the questionnaire — Design

## Decisions

- **The page is the one gate artifact; the Markdown summary and its parser go.** One check
  (regular file, 256 KiB, template marker) replaces the section and list parsing of
  `dispatcher/artifacts.py`. Tradeoff: a session without the skill blocks instead of falling
  back; that is a configuration fault we want to see.
- **The bridge trusts the iframe window reference, not an origin.** The sandboxed iframe's
  origin is `null`; `event.source === iframe.contentWindow` is the only check that cannot be
  forged from another frame. Tradeoff: none worth naming; it is two lines.
- **One message version, `v: 1`, no negotiation.** The skill pin moves the page and the
  console together. Tradeoff: an old page on a new console shows a banner instead of working.
- **Every answers message carries the full set, so the newest wins.** The console debounces
  about one second and the dispatcher applies one intent per task per pass. Tradeoff: a tab
  closed inside the debounce can lose one tick; `pagehide` flushes to shrink that window.
- **A submission is bound to the page revision it was made on (red-team 1).** The request
  route exposes `revision`: the `plan-approval` fingerprint that exists today, and for an
  `answers` request a SHA-256 of the page bytes, stored on the request when it is armed. The
  console sends `revision` with every answers post; the intent carries it; the dispatcher
  drops a submission whose `revision` is not the open request's with `intent-dropped`
  "stale revision". A draft with a stale revision is dropped too. The console polls the
  request route it already polls; when `revision` changes it reloads the iframe and shows
  "the plan changed; your selections were reset to the saved ones". Tradeoff: an operator who
  kept a tab open across a revision taps once more.
- **A submission is applied only while the task waits for the operator (red-team 2).** That is:
  parked for input or review, or at the gate before the grace park, the same condition the
  reply intent uses today. Otherwise it is dropped with "session busy" and the event log says
  so; the console shows the drop as it shows other dropped intents. Tradeoff: an "Approve"
  tapped during a rework is lost, not queued; the operator taps again when the page returns.
- **A draft never overwrites a submitted file (red-team 3).** When the file on disk has
  `submitted` not null, a draft intent is dropped with "already submitted". A submission
  always writes. The file only moves from draft to submitted, or from one submission to the
  next page revision. Tradeoff: none; a draft after a submission is stale by definition,
  because a submission either ends the request or changes the revision.
- **`restore` reads the worktree file, overlaid with the newest pending answers intent
  (red-team 4).** The request route already reads the worktree; it returns `answers` from the
  file there (empty when absent) and the web layer overlays the payload of the newest pending
  `answers` intent for the task, which it already lists for the console. This replaces the
  snapshot as the `restore` source agreed in the grill (decision 7): the snapshot is one pass
  old and the second device would overwrite the first. The snapshot still serves the
  artifacts panel and survives the worktree. Tradeoff: the web layer reads an intent payload
  it did not write; the read is bounded by the 64 KiB cap the route enforces.
- **The answers file is written only into a usable `.agent/` directory (red-team 5).** The
  dispatcher writes with the same helper that writes `stage.json`; when `.agent/` is not a
  directory the intent fails, is deleted and is logged, and nothing is created outside a
  worktree. Tradeoff: none.
- **No option texts in the file.** The page carries them; a session reads its own page. The
  file stays small and the schema stays flat. Tradeoff: the file is not readable alone after
  the page expires, which is after the task is terminal.
- **Retention of review artifacts drops from 30 to 7 days, one constant.** Tradeoff: a page
  of a task finished eight days ago is gone; its decisions are on the branch.
- **`to-questionnaire` keeps authoring the questions; `review-page` only delivers them.** One
  new skill with two modes, not two skills. Tradeoff: the spec prompt names two skills for one
  step.
- **Telegram untouched.** Its link still opens the task page, which links to the review route.

## Data model

There is no database. The records are files; the constraints are what the writer enforces
before the write and what the reader rejects.

| Record | Field | Constraint | Why |
|---|---|---|---|
| intent file (`intents/<ms>-<target>-<issue>-answers.json`) | `action` | one of the known actions, now including `answers` | `write_intent` refuses an unknown action today |
| intent file | `payload.answers` | flat object; values string, list of strings, or boolean; keys `[a-z0-9_-]{1,64}` with optional `.note` suffix, or `track`; JSON ≤ 64 KiB | the route refuses with 422 before an intent exists |
| intent file | `payload.submit` | `null`, `"changes"` or `"approve"` | the route refuses anything else |
| intent file | `payload.revision` | non-empty string | the route refuses a post without it |
| task state (`tasks/<target>/<issue>.json`) | `operator_request.fingerprint` | non-empty for `plan-approval` (exists) and for `answers` (new: SHA-256 of the page bytes) | a submission is compared against it |
| answers file (`.agent/review-answers.json`, `.agent/questionnaire-answers.json`) | `v`, `stage`, `submitted`, `submitted_at`, `actor`, `answers` | `v` is 1; `stage` matches the request kind; `submitted` as above; written atomically (temporary file, rename) | the reader is a model; it sees whole files only |
| answers file | transition | draft → draft, draft → submitted, submitted → submitted; never submitted → draft | red-team 3 |
| artifact index (`artifacts/<target>/<issue>/index.json`) | items `review-answers`, `questionnaire-answers` | registered when the file exists; `expires_at` = terminal time + 7 days | retention |
| review page (`.agent/review.html`, `.agent/questionnaire.html`) | shape | regular file inside the worktree, ≤ 256 KiB, contains `<meta name="agent-ops-review" content="1">` | the gate check |

## Identities

None. No key is widened; every record stays keyed by `target` and `issue`.

## Seams

- `dispatcher.main` pass with a stage signal in the worktree (existing): the gate check of
  the page, the bounce, and the request with its `revision`.
- `dispatcher.main._apply_intents` driven by `intents.write_intent` and the task files
  (existing drain, new `answers` action): file write, newest-wins, the four drop rules, the
  wake messages.
- `POST /api/task/{target}/{issue}/answers` (new): validation, 422, 202 with the intent name.
- `GET /api/task/{target}/{issue}/request` (existing, extended): `revision` and `answers`.
- `dispatcher.task_artifacts.collect` and `cleanup` (existing): auto-registration and the
  7-day expiry.
- Console route `/task/:target/:issue/review` (new) with the bridge, tested through the
  frontend unit suite with a fixture page in a real iframe, and the e2e suite for the phone
  width and the two themes.
- `prompts/*.md` rendered by the dispatcher (existing prompt tests): the plan, spec,
  implement and review prompts name the page, the file and the skills.
- `jesdi/general-skills` skill `review-page` (new, its own repository and pull request): the
  template; `tests/fixtures/review-page.html` in agent-ops is a copy of it.
