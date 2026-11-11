# One atomic-write helper for the dispatcher's state and settings files

## Why

Twelve write sites across eight dispatcher modules hand-roll "write a temp file, then rename it over the target" with a
fixed temp name (`<name>.tmp`, or `<name>` + `.tmp` in `state.mark_background`). Two writers to the same
path share that one temp file: one can rename the other's half-written content over the target, or
fail because the temp file vanished under it. `Path.with_suffix(".tmp")` also truncates dotted names
(`owner/repo.name-149.json` → `owner/repo.tmp`). The copies also differ on details: indent, trailing
newline, `mkdir`, file mode.

A correct helper already exists: `dispatcher.state.write_json_atomic` (`mkstemp` in the target's
directory, chmod 0644, `os.replace`, temp unlinked on any failure). Six callers use it. It only writes
compact JSON, so the other sites can't use it yet.

## For whom

The operator, and every process that shares the state dir (the dispatcher, the web console, the
hooks). After this change, a reader or a concurrent writer never sees a torn or mixed file.

## Goal

Every fixed-name temp write in `dispatcher/` goes through one `mkstemp`-based helper,
`state.write_atomic(path, str | bytes)`. `write_json_atomic` becomes a thin wrapper over it. The bytes on disk stay
exactly the same at every site.

## Non-goals

- Fsync or durability guarantees across power loss.
- Cross-process locking or read-modify-write races (`state_lock` stays as is).
- Rewriting `workspace.write_worktree_file` onto the shared helper, or changing its symlink handling
  (its docstring changes only).
- Non-atomic writes elsewhere: event-log appends and rotation (`eventlog.py`), the clone `.partial`
  rename (`onboard.py`), moves of existing files (`main.py` wake-blocked move, `messages.py` legacy
  move).
- `task_tokens.py`'s own `mkstemp` write: it already uses a random temp name and deliberately keeps
  0600 for token files. It is not in the issue's list and stays as is.
- Changing any file format, indent, key order, or trailing newline.
- A new module: both functions stay in `dispatcher/state.py`.
