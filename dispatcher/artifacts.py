"""Mechanical artifact sanity checks — no LLM judgment. A failed check
retries or fails the stage instead of propagating garbage downstream."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from dispatcher.state import read_regular

MIN_BYTES = 1500
MIN_TICKET_BYTES = 200   # a small ticket is legitimately short
TICKETS_DIR = ".agent/tickets"
PLAN_SUMMARY = ".agent/plan-review.md"   # what the operator reads at the gate
SUMMARY_MAX_BYTES = 256 * 1024          # a larger summary is not counted
# What a plan revision reads, and what the checks read: one bound per file,
# and no more ticket files than NN can number.
PLAN_FILE_MAX_BYTES = 1024 * 1024
MAX_TICKETS = 99
SPEC_FOLDER_FILES = ("spec.md", "proposal.md", "design.md")
# Markers for "no bytes to hash": a text file holds no NUL at its start.
_ABSENT, _DIRECTORY = b"\0absent", b"\0directory"

# Specs: a title plus at least two H2 sections (a bug diagnosis satisfies
# this too). Tickets follow to-tickets' per-file template.
SPEC_PATTERNS = [r"^# .+", r"^## .+", r"^## .+[\s\S]*^## .+"]
TICKET_PATTERNS = [r"(?i)what to build", r"(?im)^\W*blocked by", r"(?m)^- \[ \] "]
_TICKET_NAME = re.compile(r"^(\d{2})-[a-z0-9][a-z0-9._-]*\.md$")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""
    count: int = 0   # tickets in a valid set


def _check(path: str | Path, patterns: list[str],
           min_bytes: int = MIN_BYTES) -> CheckResult:
    p = Path(path)
    if not p.exists():
        return CheckResult(False, f"artifact missing: {p}")
    raw = read_regular(p, PLAN_FILE_MAX_BYTES)   # never follows, never blocks
    if raw is None:
        return CheckResult(False, f"artifact is not a readable regular file "
                                  f"(a symlink, a pipe, or too large): {p}")
    text = raw.decode(errors="replace")
    if len(text.encode()) < min_bytes:
        return CheckResult(False, f"artifact too small (<{min_bytes}B): {p}")
    for pat in patterns:
        if not re.search(pat, text, re.MULTILINE):
            return CheckResult(False, f"required heading/pattern not found ({pat}): {p}")
    return CheckResult(True)


_SUMMARY_SECTIONS = ["tickets", "open questions", "corrections"]
_LIST_ITEM = re.compile(r"(?:[-*]|\d+\.) ")
# Every form Markdown reads as a heading, and a little more (fail closed): up
# to three spaces of indent, a tab after the hashes, an empty heading; and a
# text line underlined with `===` or `---` (setext), which the prescribed
# summary never has.
_H1 = r"(?m)^ {0,3}#(?!#)"
_H2 = r"(?m)^ {0,3}##(?!#)[ \t]*"
_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*")


def _has_setext_heading(text: str) -> bool:
    """A text line with an underline of `=` or `-` below it. Line by line:
    one pattern over the whole text is quadratic on a very long line."""
    lines = text.split("\n")
    return any(_UNDERLINE.fullmatch(line) and lines[i].strip()
               for i, line in enumerate(lines[1:]))


def _open_questions_lines(summary: str | Path) -> list[str] | None:
    """The non-blank lines of the summary's open-questions section. None for
    a summary that is not the prescribed one: not a readable regular file
    (state.read_regular: a FIFO never blocks the pass), above the size cap,
    more than one title, level-2 headings other than the three, in order, or
    a setext heading."""
    raw = read_regular(summary, SUMMARY_MAX_BYTES)
    try:
        text = raw.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):   # AttributeError: not readable
        return None
    headings = [h.strip().lower() for h in re.findall(_H2 + r"(.*)$", text)]
    if (headings != _SUMMARY_SECTIONS or len(re.findall(_H1, text)) > 1
            or _has_setext_heading(text)):
        return None
    section = re.split(_H2 + r".*$", text)[2]
    return [ln.rstrip() for ln in section.splitlines() if ln.strip()]


def count_open_questions(summary: str | Path) -> int | None:
    """The entries of the plan summary's open-questions section: its top-level
    list items, or 0 when it says `None.`. None = cannot tell (see
    _open_questions_lines, or anything but a list in the section): the
    caller treats that as "questions are open"."""
    lines = _open_questions_lines(summary)
    if lines == ["None."]:
        return 0
    if not lines or not _LIST_ITEM.match(lines[0]):
        return None
    count = 0
    for ln in lines:
        if _LIST_ITEM.match(ln):
            count += 1
        elif not ln[0].isspace():   # not a continuation of the item above
            return None
    return count


def check_spec(path: str | Path) -> CheckResult:
    return _check(path, SPEC_PATTERNS)


def ticket_files(tickets_dir: str | Path) -> list[Path]:
    """NN-slug.md files in numeric order — the execution order."""
    d = Path(tickets_dir)
    if not d.is_dir():
        return []
    # By name alone: an entry that is no regular file (a symlink, a pipe, a
    # directory) fails the check of its content instead of being skipped.
    named = [(int(m.group(1)), p) for p in d.iterdir()
             if (m := _TICKET_NAME.match(p.name))]
    return [p for _, p in sorted(named, key=lambda t: (t[0], t[1].name))]


def check_tickets(tickets_dir: str | Path) -> CheckResult:
    """A valid set: ≥1 ticket, numbers contiguous from 01 with no duplicates,
    every file carrying what-to-build, blocked-by and an unchecked criterion."""
    d = Path(tickets_dir)
    if not d.is_dir():
        return CheckResult(False, f"tickets dir missing: {d}")
    files = ticket_files(d)
    if not files:
        return CheckResult(False, f"no ticket named NN-slug.md in {d}")
    if len(files) > MAX_TICKETS:
        return CheckResult(False, f"more than {MAX_TICKETS} ticket files in {d}")
    numbers = [int(p.name[:2]) for p in files]
    if len(set(numbers)) != len(numbers):
        dup = sorted({n for n in numbers if numbers.count(n) > 1})
        return CheckResult(False, f"duplicate ticket number(s) {dup} in {d}")
    if numbers != list(range(1, len(numbers) + 1)):
        return CheckResult(False, f"ticket numbers must be contiguous from 01, got {numbers} in {d}")
    for p in files:
        r = _check(p, TICKET_PATTERNS, min_bytes=MIN_TICKET_BYTES)
        if not r.ok:
            return r
    return CheckResult(True, count=len(files))


class PlanRevision(NamedTuple):
    """What the operator is asked to approve at the plan gate. `fingerprint`
    is "" when the revision cannot be told; `problem` then says why."""
    path: str            # the summary, worktree-relative
    fingerprint: str
    problem: str = ""


def _summary_path(root: Path, artifact: str) -> str:
    """stage.json is model-written, so a missing path or one outside the
    worktree falls back to the path the plan prompt names."""
    try:
        rel = (root / artifact).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError, RuntimeError):
        rel = "."
    return PLAN_SUMMARY if rel == "." else rel


def _spec_folder_files(root: Path, spec_path: str) -> list[Path]:
    """spec.md, proposal.md and design.md beside the task's spec; none for a
    task with no spec path or one outside the worktree."""
    try:
        folder = (root / spec_path).parent
        folder.resolve().relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return []
    return [folder / name for name in SPEC_FOLDER_FILES] if spec_path else []


def _read_plan_file(p: Path) -> bytes | None:
    """The file's bytes, b"" marked as absent for a file that is not there
    (each file of a revision is optional), None for one that is there and is
    not a readable regular file of a sane size."""
    try:
        if stat.S_ISDIR(os.lstat(p).st_mode):
            return _DIRECTORY   # nothing to read, nothing to follow
    except FileNotFoundError:
        return _ABSENT
    except (OSError, ValueError):
        return None
    try:
        return read_regular(p, PLAN_FILE_MAX_BYTES)
    except OSError:
        return None


def plan_revision(worktree: str | Path, artifact: str,
                  spec_path: str = "") -> PlanRevision:
    """The plan revision on disk: the summary's path, and a digest over the
    summary, the ticket files and the spec folder's three files (names and
    bytes). Every file is read without following a symlink and without
    blocking, up to a size limit. Never raises: a file that is there and
    cannot be read that way makes the revision unavailable."""
    root = Path(worktree)
    rel = _summary_path(root, artifact)
    tickets = ticket_files(root / TICKETS_DIR)
    if len(tickets) > MAX_TICKETS:
        return PlanRevision(rel, "", f"more than {MAX_TICKETS} ticket files")
    digest = hashlib.sha256()
    for p in [root / rel, *tickets, *_spec_folder_files(root, spec_path)]:
        data = _read_plan_file(p)
        if data is None:
            return PlanRevision(rel, "", (
                f"{p} is not a readable regular file (a symlink, a pipe, or "
                f"above {PLAN_FILE_MAX_BYTES // 1024} KB)"))
        digest.update(f"{p.name}\0{len(data)}\0".encode() + data)
    return PlanRevision(rel, digest.hexdigest())
