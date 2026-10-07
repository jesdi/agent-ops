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
REVIEW_PAGE = ".agent/review.html"      # what the operator reads at the gate
REVIEW_PAGE_MAX_BYTES = 256 * 1024      # a larger page is bounced
REVIEW_PAGE_MARKER = '<meta name="agent-ops-review" content="1">'
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


_QUESTION_BLOCK = re.compile(r'\bdata-q="[a-z0-9_-]{1,64}"')


def _page_problem(page: str | Path) -> tuple[str, str]:
    """(problem, text) of a review page: the problem is "" for a regular file
    (state.read_regular: a FIFO never blocks the pass) of at most 256 KiB
    that carries the template marker."""
    try:
        size = os.lstat(page).st_size   # no follow: a symlink is read below, and refused
    except (OSError, ValueError):
        return "review page missing", ""
    if size > REVIEW_PAGE_MAX_BYTES:
        return "review page exceeds 256 KiB", ""
    try:
        raw = read_regular(page, REVIEW_PAGE_MAX_BYTES)
    except OSError:   # the page went away between the check and the read
        raw = None
    if raw is None:
        return ("review page is not a readable regular file "
                "(a symlink, a pipe, a directory, or it grew)"), ""
    text = raw.decode("utf-8", errors="replace")
    if REVIEW_PAGE_MARKER not in text:
        return "review page lacks the template marker", ""
    return "", text


def count_open_questions(page: str | Path) -> int | None:
    """The question blocks (`data-q="id"`) of the review page. None = cannot
    tell (the page fails the shape check): the caller treats that as
    "questions are open"."""
    problem, text = _page_problem(page)
    return None if problem else len(_QUESTION_BLOCK.findall(text))


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
    path: str            # the review page, worktree-relative
    fingerprint: str
    problem: str = ""


def _page_path(root: Path, artifact: str) -> str | None:
    """The artifact, worktree-relative and not resolved (the read refuses a
    symlink); the prescribed page for an empty one, None when it leaves the
    worktree (stage.json is model-written)."""
    try:
        (root / artifact).resolve().relative_to(root.resolve())
        rel = Path(os.path.normpath(root / artifact)).relative_to(
            os.path.normpath(root)).as_posix()
    except (OSError, ValueError, RuntimeError):
        return None
    return REVIEW_PAGE if rel == "." else rel


def _spec_folder_files(root: Path, spec_path: str) -> list[Path] | None:
    """spec.md, proposal.md and design.md beside the task's spec; none for a
    task with no spec path. None when the folder leaves the worktree (a
    symlink included): its files cannot be covered, so the revision cannot
    be told."""
    if not spec_path:
        return []
    try:
        folder = (root / spec_path).parent
        folder.resolve().relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    return [folder / name for name in SPEC_FOLDER_FILES]


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
    """The plan revision on disk: the review page's path, and a digest over the
    page, the ticket files and the spec folder's three files (names and
    bytes). Every file is read without following a symlink and without
    blocking, up to a size limit. Never raises: a file that is there and
    cannot be read that way makes the revision unavailable."""
    root = Path(worktree)
    rel = _page_path(root, artifact)
    if rel is None:
        return PlanRevision(artifact, "", "review page is outside the worktree")
    problem, _ = _page_problem(root / rel)
    if problem:
        return PlanRevision(rel, "", problem)
    tickets = ticket_files(root / TICKETS_DIR)
    if len(tickets) > MAX_TICKETS:
        return PlanRevision(rel, "", f"more than {MAX_TICKETS} ticket files")
    spec_files = _spec_folder_files(root, spec_path)
    if spec_files is None:
        return PlanRevision(rel, "", f"the spec folder of {spec_path} is outside the worktree")
    digest = hashlib.sha256()
    for p in [root / rel, *tickets, *spec_files]:
        data = _read_plan_file(p)
        if data is None:
            return PlanRevision(rel, "", (
                f"{p} is not a readable regular file (a symlink, a pipe, or "
                f"above {PLAN_FILE_MAX_BYTES // 1024} KB)"))
        digest.update(f"{p.name}\0{len(data)}\0".encode() + data)
    return PlanRevision(rel, digest.hexdigest())
