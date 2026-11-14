# 0014. Dispatcher state files use one path-based atomic writer

Date: 2026-11-14

## Context

Fixed-name temporary files let concurrent writers collide, and `with_suffix` can
truncate a dotted target name. State and settings files need one write path.
Worktree files have a different boundary: the writer must refuse symlinks.

## Decision

- `dispatcher.state.write_atomic(path, str | bytes)` owns path-based whole-file
  replacement for dispatcher state and settings files. It creates the parent,
  writes through a unique sibling temporary file, and renames over the target.
- `write_json_atomic(path, doc, *, indent=None, sort_keys=False)` serializes with
  `json.dumps` and calls `write_atomic`. Callers select the existing JSON format
  with those arguments; they do not implement another temporary-file writer.
- `workspace.write_worktree_file` remains a separate dir-fd writer with
  `O_NOFOLLOW`. A path-based helper cannot provide its symlink guarantee.

## Consequences

The dispatcher has one path-based atomic-write implementation. A later state
write should use it; a worktree write should keep the symlink-safe boundary.
