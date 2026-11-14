# 0016. Atomic state writes use unique hidden sibling temp files

Date: 2026-11-14

## Context

One fixed `<name>.tmp` makes writers share a temporary file. A temporary file
in the target directory is needed for an atomic rename on the same filesystem.

## Decision

- The writer uses `mkstemp` names shaped `.<name>.<random>.tmp` in the target's
  directory. It removes the temp file after a write, chmod, or rename failure.
- A process killed between temp creation and rename can leave a hidden file.
  No sweeper is added. The operator accepted this at the plan gate; existing
  `write_json_atomic` callers already have that behavior.
- The name can be about 14 bytes longer than the target. A target near the
  filesystem's name-length limit may fail with `OSError`; that limit is accepted.

## Consequences

Concurrent writers do not share a temp file and readers of the target see
whole files. A crash can leave a hidden sibling file, so directory readers
must continue selecting by their intended file patterns.
