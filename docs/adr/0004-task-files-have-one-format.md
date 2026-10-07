# 0004. Task files have one format; retired fields are dropped on load

Date: 2026-10-07

## Context

Task state is one JSON file per task, read by one loader. A change to the
pipeline can retire a field, a stage value or a request kind while task files
written by the old code are on disk.

## Decision

- The dispatcher supports one task format. It carries no code path for an
  older pipeline.
- A retired FIELD is dropped by the loader (`d.pop(name, None)`), so an old
  file still loads. A loader that rejected it would skip every old file,
  open pull requests included, with no sign.
- A retired stage value or request kind is NOT converted by the loader. A
  one-time migration command rewrites the files, with the dispatcher
  stopped, under the pass lock, all-or-nothing, and safe to run twice. Tasks
  in a stage the new code cannot continue are drained by the operator before
  the deploy.
- A migration never writes a state that reads as an approval, and every
  value it cannot classify stops the run.
- The migration module, its start guard and its prompt text are deleted
  after the deploy.

## Consequences

- A new field needs a default that is safe for a file written before it
  existed. For a field that gates a control, the safe default is the strict
  one.
- A deploy that retires a stage needs a drain step and a migration step in
  its notes.
- The dispatcher writes into a task worktree and into the clone only through
  the helpers that never follow a session's symbolic link; a session can
  write both directories.
