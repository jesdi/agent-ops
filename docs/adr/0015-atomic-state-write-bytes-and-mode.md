# 0015. Atomic state writes preserve bytes and set mode 0644

Date: 2026-11-14

## Context

The dispatcher, console, and hooks share state files with established byte
formats. Existing text writes use the interpreter's default encoding, and the
files are readable across units.

## Decision

- `write_atomic` writes string data in text mode with the interpreter's
  default encoding, matching `Path.write_text`; bytes data is written as is.
- JSON callers keep their existing indentation, key order, and lack of a
  trailing newline. JSONL callers keep one newline after every message.
- The helper sets mode 0644 before replacing the target, regardless of umask
  or the target's previous mode. It calls `os.replace(tmp, path)` with two
  positional arguments.

## Consequences

A writer migration must verify byte-for-byte output and 0644 mode. A later
format or permission change needs its own explicit decision and tests.
