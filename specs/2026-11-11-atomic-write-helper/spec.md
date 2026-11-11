# One atomic-write helper for the dispatcher's state and settings files — Spec

Source: issue #149 (design rewritten in the 2026-11-05 triage). Operator answer on 2026-11-11
(questionnaire `dump-test`: `retarget`). Track: standard.

## Settled design (from the issue, carried over)

1. In `dispatcher/state.py`, replace the body of `write_json_atomic` with a general
   `write_atomic(path: Path, data: str | bytes) -> None`. It keeps the same `mkstemp` in the target's
   directory, `mkdir(parents=True)`, chmod 0644, `os.replace`, and temp unlink on any failure, and
   opens in binary mode when `data` is `bytes`. `write_json_atomic(path, doc, *, indent=None,
   sort_keys=False)` becomes a thin wrapper that calls `write_atomic(path, json.dumps(doc,
   indent=indent, sort_keys=sort_keys))`. Existing callers keep working with no changes. No new
   module.
2. Route each site below through the helper, with settings that keep the on-disk bytes unchanged. No
   JSON site gains a trailing newline.
3. Drop the now-redundant `mkdir(parents=True, exist_ok=True)` lines and temp-name variables at each
   site. Keep `main._write_heartbeat`'s `try/except OSError` and `task_artifacts._write`'s
   "unchanged, skip" check (the text is compared before writing).
4. File mode: every site ends up 0644. The helper chmods explicitly instead of relying on the umask.
5. Each module imports `write_atomic` / `write_json_atomic` from `dispatcher.state`. If an import
   cycle appears, move the two functions to the top of `state.py`; still no new module.
6. `workspace.write_worktree_file` stays dir-fd based (`O_NOFOLLOW`, `mode=None`). Leave its logic
   alone, and update its docstring to say it is the symlink-safe variant of `state.write_atomic`.

### Sites, as of main `6afac40` (find them by function name)

| Site | Call after the change |
|---|---|
| `base_line.record_wait` | `write_json_atomic(p, {...})` (compact) |
| `execution_overrides.save` | `write_json_atomic(path, ..., indent=2)` |
| `intents.Queue._write` | `write_json_atomic(p, {...}, indent=2)` |
| `main._write_heartbeat` (`pass.json`) | `write_json_atomic(p, {...})` (compact), inside the existing `try/except OSError` |
| `messages.mark_delivered` | `write_atomic(p, "".join(_dump(m) + "\n" for m in kept))` |
| `messages._merge_legacy` | `write_atomic(dest, "".join(_dump(m) + "\n" for m in merged))` |
| `state._write_task` | `write_json_atomic(p, d, indent=2)` |
| `state.mark_background` | `write_json_atomic(p, {...})` (compact) |
| `task_artifacts._write` | `write_atomic(path, text)`, where `text` is the `indent=2, sort_keys=True` dump it already builds for the skip check |
| `task_artifacts._copy_content` | `write_atomic(dest, content)` (bytes) |
| `triage.enqueue` | `write_json_atomic(p, {...}, indent=2)` |
| `triage.save_cursors` | `write_json_atomic(p, cursors, indent=2, sort_keys=True)` |

## What moved in the code under the design

- Line numbers have moved since `61dbbdb` (the heartbeat is now near `main.py:2595`,
  `mark_background` near `state.py:725`). Every listed site still exists with the same shape, and
  `write_json_atomic` still has the body the issue describes. Nothing else in the design changes.
- **Contradiction, resolved by the operator.** `tests/test_priority_file.py::test_a_failed_save_leaves_no_temp_file_and_the_old_mode[dump]`
  breaks a save by patching `json.dump`. Once `write_json_atomic` uses `json.dumps`, that patch no
  longer hits. The operator chose `retarget`: that one test's `"dump"` case patches `json.dumps`
  instead. This is the only allowed edit to an existing test. Its assertions stay the same.
- Temp names change from `<name>.tmp` to `.<name>.<random>.tmp` (dotfiles). The directory readers
  were checked: `intents` globs `*.json`, `messages.undelivered_counts` globs `*.jsonl`, and
  `tests/test_messages.py` globs `*.tmp`. A finished write leaves no temp file, so all of them still
  hold. `task_artifacts` expiry globs `content/*` and deletes; it could delete an in-flight temp, the
  same as today with `<name>.tmp`, so nothing gets worse.
- `task_tokens.py` already has its own `mkstemp` write (random name, 0600 on purpose for tokens). The
  issue doesn't list it and it doesn't use a fixed name, so it is left alone (see Out of scope). The
  acceptance grep still passes, since it targets fixed `.tmp` names only.
- Some tests count or fail `os.replace` calls (`test_answers_intent_acceptance`,
  `test_session_record_errors`, `test_priority_file`, `test_usage_providers`). `write_atomic` calls
  `os.replace(tmp, path)` with two positional arguments, as `write_json_atomic` does today, so those
  spies keep working.

## Requirements

1. `state.write_atomic(path, data)` writes `data` to `path` atomically. It accepts `str` (written as
   text) and `bytes` (written as is), and creates missing parent directories.
2. The file `write_atomic` leaves has mode 0644, whatever the process umask.
3. A failure while writing, chmodding or renaming leaves no temp file in the target's directory,
   leaves the previous target content untouched, and re-raises the error.
4. Concurrent writers never share a temp file. Many threads, and two processes, writing distinct
   large payloads to one path: every read of the target equals exactly one whole payload, and no
   temp files remain afterwards.
5. `write_json_atomic(path, doc)` keeps its current output for every existing caller (compact JSON,
   no trailing newline). With `indent=` / `sort_keys=` it writes exactly
   `json.dumps(doc, indent=..., sort_keys=...)`.
6. Each of the twelve sites in the table writes byte-identical output to before for the same input,
   with mode 0644.
7. No fixed-name temp file remains in `dispatcher/`:
   `git grep -nE 'with_suffix\("\.tmp"\)|\+ "\.tmp"|\.tmp"\)' -- 'dispatcher/*.py'` finds nothing.
   The only temp-file code left is `state.write_atomic`, `workspace.write_worktree_file` and the
   out-of-scope `task_tokens` writer.
8. Behaviour that the sites keep: `main._write_heartbeat` still swallows an `OSError` with its
   warning and never raises into a pass, and `task_artifacts._write` still skips the write when the
   on-disk text already equals the new text.
9. `workspace.write_worktree_file`'s logic is unchanged. Its docstring names it the symlink-safe
   variant of `state.write_atomic`.
10. The existing suite passes. The only edit to an existing test is the `json.dump` → `json.dumps`
    patch target in `tests/test_priority_file.py`. Every other test change is an addition.

## Scenarios

### Scenario: text payload (R1)
- **Given** an empty `tmp_path`
- **When** `write_atomic(tmp_path / "a" / "b.json", '{"k": 1}')`
- **Then** `tmp_path/a/b.json` exists and its bytes are exactly `{"k": 1}`, and `tmp_path/a` holds only `b.json`

### Scenario: bytes payload (R1)
- **Given** an empty `tmp_path`
- **When** `write_atomic(tmp_path / "blob.bin", b"\x00\xff\n")`
- **Then** the file's bytes are exactly `b"\x00\xff\n"`

### Scenario: dotted file name keeps its name (R1)
- **Given** an empty `tmp_path`
- **When** `write_atomic(tmp_path / "owner-repo.name-149.json", "x")`
- **Then** the directory holds exactly `owner-repo.name-149.json` with content `x`, and no `owner-repo.tmp`

### Scenario: mode under a strict umask (R2)
- **Given** the process umask is `0o077`
- **When** `write_atomic(tmp_path / "f", "x")`
- **Then** `stat(f).st_mode & 0o777 == 0o644`

### Scenario: failed rename (R3)
- **Given** `tmp_path/f` holds `old`, and `os.replace` is patched to raise `OSError("disk full")`
- **When** `write_atomic(tmp_path / "f", "new")`
- **Then** `OSError` is raised, `f` still reads `old`, and the directory holds only `f`

### Scenario: failed write (R3)
- **Given** `tmp_path/f` holds `old`, and writing to the temp file raises `OSError` (for example `os.fdopen` patched to return a file whose `write` raises)
- **When** `write_atomic(tmp_path / "f", "new")`
- **Then** `OSError` is raised, `f` still reads `old`, and the directory holds only `f`

### Scenario: serialization failure through the JSON wrapper (R3, R10)
- **Given** `priority.save` has written `anthropic`, and `json.dumps` is patched to raise `OSError`
- **When** `priority.save(..., "openai", ...)`
- **Then** `OSError` is raised, the directory holds only the priority file, and `load` returns `anthropic` (the retargeted existing test)

### Scenario: threads never tear the file (R4)
- **Given** 8 threads, each with its own 1 MiB payload of a distinct repeated byte
- **When** each thread calls `write_atomic(path, payload)` 50 times, and a reader thread repeatedly reads `path` while they run
- **Then** every read that finds the file equals exactly one of the 8 payloads, the final content is one of them, and the directory holds only the target file

### Scenario: two processes never tear the file (R4)
- **Given** 2 processes, each with its own 1 MiB payload
- **When** each calls `write_atomic(path, payload)` 50 times concurrently
- **Then** the file, read after each write and at the end, equals exactly one payload, and the directory holds only the target file

### Scenario: wrapper default unchanged (R5)
- **When** `write_json_atomic(p, {"b": 1, "a": [1, 2]})`
- **Then** the bytes are exactly `{"b": 1, "a": [1, 2]}`: compact, insertion order, no trailing newline

### Scenario: wrapper with indent and sort_keys (R5)
- **When** `write_json_atomic(p, {"b": 1, "a": 2}, indent=2, sort_keys=True)`
- **Then** the bytes equal `json.dumps({"b": 1, "a": 2}, indent=2, sort_keys=True)`, with no trailing newline

### Scenario: sites keep their bytes (R6)
- **Given** each site in the table, called through its public function with a fixed input (for example `triage.save_cursors(d, {"z": "…", "a": "…"})`, `execution_overrides.save(...)`, `messages.mark_delivered(...)`, `task_artifacts._copy_content(dest, b"\x00raw")`)
- **When** the site writes
- **Then** the file's bytes equal the format in the table (compact / `indent=2` / `sort_keys=True` / newline-terminated JSONL / raw bytes) exactly as computed before the change, the mode is 0644, and no `*.tmp` file is left in the directory

### Scenario: dotted repository in mark_background (R6)
- **Given** a repository `owner/repo.name`
- **When** `state.mark_background(state_dir, "owner/repo.name", 149, tasks)`
- **Then** the background file reads back through `read_background`, and its directory holds no temp file

### Scenario: no fixed temp names left (R7)
- **When** the acceptance `git grep` from R7 runs over `dispatcher/*.py`
- **Then** it prints nothing (exit status 1)

### Scenario: heartbeat still never raises (R8)
- **Given** `write_atomic` (as seen from `main`) raises `OSError`
- **When** `_write_heartbeat(cfg, started_at)` runs
- **Then** it returns normally and prints `[warn] heartbeat write failed:` to stderr

### Scenario: artifact index unchanged is not rewritten (R8)
- **Given** `task_artifacts._write(path, value)` has run once
- **When** it runs again with an equal `value` and `os.replace` is spied
- **Then** `os.replace` is not called and the file's mtime is unchanged

### Scenario: worktree writer untouched (R9)
- **When** the existing `tests/test_workspace.py` symlink and temp-file tests run
- **Then** they pass, and `write_worktree_file`'s docstring mentions `state.write_atomic`

### Scenario: suite green with one retargeted test (R10)
- **When** `make gate` runs
- **Then** it passes, and `git diff origin/main -- tests/` shows only additions apart from the `json.dump` → `json.dumps` change in `tests/test_priority_file.py`

## Out of scope

- Fsync / power-loss durability, and cross-process locking (`state_lock` unchanged).
- Moving `workspace.write_worktree_file` onto the helper.
- `task_tokens.py`'s 0600 `mkstemp` writer, event-log appends and rotation, the clone `.partial`
  rename, and moves of existing files.
- Any change of file format, indent, key order or trailing newline.
