"""Mechanical artifact sanity checks — no LLM judgment. A failed check
retries or fails the stage instead of propagating garbage downstream."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MIN_BYTES = 1500
MIN_TICKET_BYTES = 200   # a small ticket is legitimately short
TICKETS_DIR = ".agent/tickets"
PLAN_SUMMARY = ".agent/plan-review.md"   # what the operator reads at the gate
SUMMARY_MAX_BYTES = 256 * 1024          # a larger summary is not counted

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
    text = p.read_text(errors="replace")
    if len(text.encode()) < min_bytes:
        return CheckResult(False, f"artifact too small (<{min_bytes}B): {p}")
    for pat in patterns:
        if not re.search(pat, text, re.MULTILINE):
            return CheckResult(False, f"required heading/pattern not found ({pat}): {p}")
    return CheckResult(True)


_SUMMARY_SECTIONS = ["tickets", "open questions", "corrections"]
_LIST_ITEM = re.compile(r"(?:[-*]|\d+\.) ")


def _open_questions_lines(summary: str | Path) -> list[str] | None:
    """The non-blank lines of the summary's open-questions section. None for
    a summary that is not the prescribed one: unreadable, above the size cap,
    more than one title, or `## ` headings other than the three, in order."""
    try:
        with open(summary, "rb") as fh:
            raw = fh.read(SUMMARY_MAX_BYTES + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    headings = [h.strip().lower() for h in re.findall(r"(?m)^## +(.*)$", text)]
    if (len(raw) > SUMMARY_MAX_BYTES or headings != _SUMMARY_SECTIONS
            or len(re.findall(r"(?m)^# ", text)) > 1):
        return None
    section = re.split(r"(?m)^## +.*$", text)[2]
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
    named = [(int(m.group(1)), p) for p in d.iterdir()
             if (m := _TICKET_NAME.match(p.name)) and p.is_file()]
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
