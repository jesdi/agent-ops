# Answers artifact: the answered page, kept in the box archive — Design

## Decisions

- **The answered page is rendered on the box, in markup, not by script** — the
  request page is rewritten with the stdlib HTML parser (checked radios, note
  text, `disabled` on every control, an appended plain-text block for unmatched
  answers); it reads right with scripts off and no page script is trusted to
  restore answers. Tradeoff: end tags are re-emitted in lower case; the page
  is otherwise byte-identical outside the touched tags.
- **The template script stays in the answered page** — it only paints the
  "N of M answered" bar from `checked`; disabled controls fire no `change`, and
  the artifact route's CSP (`connect-src 'none'`, `frame-ancestors 'none'`)
  already keeps it from posting or being embedded. Tradeoff: we rely on the
  route's sandbox for "nothing can be posted", not on stripping scripts.
- **Capture lives in `task_artifacts`, called from the drain** — after
  `answers.record` writes a submission, `operator_intents._apply_answers_intent`
  calls the capture, inside a guard that logs a warning and swallows any
  failure (requirement 9). `answers.py` stays free of archive knowledge.
- **One capture function serves the drain and the collector** — it reads the
  answers file back from the worktree, ignores anything that is not a
  submission, and skips when the stored item already holds that file. The
  collector calls it for both kinds; this is how a session's text answer gets
  captured (requirement 6) and how a failed drain capture gets a second chance
  (open question `retry` on the plan review: the retry reads the page later than
  the drain; the alternative limits the collector to text answers).
- **"Already captured" is the answers file's SHA-256 in `StoredArtifact.digest`**
  — for the two answers ids, `digest` names the source answers bytes, not the
  stored HTML. No new index field, no format change. Tradeoff: one field means
  two things by id. Legacy raw-JSON items carry the same digest of the same
  file, so they are not rebuilt (matches the non-goal).
- **The collector stops registering the answers files and ignores the two ids
  in the manifest** — otherwise its raw copy overwrites the capture
  (requirement 8). A session's entry under another id stays a plain copy.
- **Page source** — the request page of the open request when its kind is the
  answers file's kind, else the kind's default page; read with
  `state.read_regular` under the 256 KiB page cap (no symlinks), decoded as
  UTF-8; a decode or read failure is a capture failure.
- **Write order: content, then index** — the content file is replaced
  atomically, then the index. ponytail: an index write that fails after the
  content write leaves new content under old metadata; acceptable on a disk
  that just refused a write.
- **The collector's capture works on the pass's in-memory index** — `collect`
  loads the index once and saves it once; the capture must use that object, not
  load and save its own, or the outer save drops it. The drain capture loads,
  updates (`retain_for` included) and saves on its own. Both run in the one
  dispatcher process, drain at the top of the pass and collection at the end,
  so they never interleave.

## Data model

No database. The archive index `<state_dir>/artifacts/<key>/index.json` and
`content/<id>` keep their format.

| Table | Field | Constraint | Why |
|---|---|---|---|
| index item `review-answers` / `questionnaire-answers` | `media_type` | always `text/html` when written by the capture | requirement 1 |
| same | `path` | the answers file's worktree path | requirement 5 |
| same | `digest` | SHA-256 of the captured answers file bytes | skip re-capture (requirement 8) |
| same | `id` | at most one item per id; written only by the capture | requirements 8, reserved ids |

## Identities

None.

## Seams

- `operator_intents._apply_intents` driving an `answers` intent, then
  `GET /api/task/{repository}/{issue}/artifacts/{id}` — existing (the
  reproduction test's path).
- `task_artifacts.collect(state_dir, task, repo, publish=False)` — existing,
  for the collector's behaviour (text answers, reserved ids, no rebuild).
- `task_artifacts.cleanup(state_dir, now=...)` — existing, for retention.
