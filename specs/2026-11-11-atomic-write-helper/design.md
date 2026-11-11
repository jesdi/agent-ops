# One atomic-write helper for the dispatcher's state and settings files — Design

## Decisions

- **One path-based helper, `state.write_atomic(path, str | bytes)`, in `dispatcher/state.py`** — every
  fixed-name site shares one tested implementation; the symlink-safe dir-fd writer in `workspace.py`
  stays separate because a path-based helper cannot give its `O_NOFOLLOW` guarantee.
- **`write_json_atomic(path, doc, *, indent=None, sort_keys=False)` serialises with `json.dumps` and
  calls `write_atomic`** — one code path for JSON and raw writes; the cost is the retargeted
  `json.dump` patch in `tests/test_priority_file.py`, which the operator chose (`retarget`).
- **Text is written in text mode with the interpreter's default encoding** — exactly what
  `Path.write_text` does at the sites today, so the on-disk bytes cannot change; forcing UTF-8 would
  be a format change the spec rules out.
- **Mode is always set to 0644 before the rename** — a target that was another mode (say 0600) ends
  0644; the issue states 0644 is what every site gets today under the default umask and needs.
- **`os.replace(tmp, path)` keeps two positional arguments** — existing test spies on `os.replace`
  (`test_session_record_errors`, `test_answers_intent_acceptance`, `test_priority_file`,
  `test_usage_providers`) keep matching.
- **Temp names become `.<name>.<random>.tmp` dotfiles in the target's directory** (red-team: directory
  readers) — no reader pattern in `dispatcher/` (`task-*.json`, `*.json`, `*.jsonl`, `task-*.txt`,
  the wake-blocked prefix, `*/index.json`) matches a name that starts with a dot and ends in `.tmp`;
  `task_artifacts`' `content/*` expiry can delete an in-flight temp, as it can delete `<name>.tmp`
  today, and the writer then raises (unchanged risk).
- **A writer killed between `mkstemp` and the rename leaves its hidden temp file behind**
  (red-team: crash leftovers) — today the next write reuses and overwrites the one `<name>.tmp`, after
  this change each crash can leave one more dotfile. Accepted without a sweeper, as
  `write_json_atomic` already behaves; see the open question on the review page.
- **Long names: the temp name is ~14 characters longer than the target name** (red-team: `NAME_MAX`)
  — the longest dispatcher state names (`<owner>-<repo>-<issue>.json` and similar) are far below the
  255-byte limit; a name within 14 bytes of it would now fail with `OSError`, accepted.
- **No new locking** (red-team: read-modify-write) — two writers still race on content
  (last rename wins); `state_lock` stays the guard for read-modify-write, as the non-goals say.
- **Site migrations land in two tickets after the helper** — JSON sites, then the text and bytes
  sites with the acceptance grep, so each review stays small and the grep is checked once every site
  has moved.

## Data model

No database. Files and their on-disk format, unchanged by this change:

| Table | Field | Constraint | Why |
|---|---|---|---|
| `task-*.json`, `background/*`, `base-line/*`, `execution-overrides/*`, `intents/*`, `pass.json`, triage request and cursors, artifact `index.json` | whole file | bytes identical to before for the same input; mode 0644 | other processes (web console, hooks) parse them; R5, R6 |
| `messages/*.jsonl` | whole file | one `json.dumps` line per message, each ending `\n` | append and read code splits on lines; R6 |
| artifact `content/*` | whole file | raw bytes as given | served back verbatim; R6 |
| any target's directory | temp files | none left after a finished or failed write | readers and tests glob these directories; R3, R4 |

## Identities

None. No key widens: target paths are built exactly as today; only the temp-file name changes, and
nothing reads it.

## Seams

- `dispatcher.state.write_atomic(path: Path, data: str | bytes) -> None` — new.
- `dispatcher.state.write_json_atomic(path: Path, doc, *, indent: int | None = None, sort_keys: bool = False) -> None`
  — existing, widened with two keyword arguments.
- The public functions of each site — existing: `base_line.record_wait`, `execution_overrides.save`,
  `intents.Queue.write`, `main._write_heartbeat`, `messages.mark_delivered`,
  `messages.migrate_legacy` (reaches `_merge_legacy`), `state.save` and `state.archive` (reach
  `_write_task`), `state.mark_background`, `task_artifacts.ArtifactIndex.save` (reaches `_write`),
  `task_artifacts.collect` (reaches `_copy_content`; the spec's scenarios may call `_write` and
  `_copy_content` directly), `triage.enqueue`, `triage.save_cursors`.
- The acceptance command `git grep -nE 'with_suffix\("\.tmp"\)|\+ "\.tmp"|\.tmp"\)' -- 'dispatcher/*.py'`
  — existing (R7).
