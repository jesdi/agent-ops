"""Mechanical artifact sanity checks — no LLM judgment. A failed check
retries or fails the stage instead of propagating garbage downstream."""
from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

MIN_BYTES = 1500
MIN_TICKET_BYTES = 200   # a small ticket is legitimately short
TICKETS_DIR = ".agent/tickets"
PLAN_SUMMARY = ".agent/plan-review.md"   # what the operator reads at the gate

# Specs: a title plus at least two H2 sections (a bug diagnosis satisfies
# this too). Tickets follow to-tickets' per-file template.
SPEC_PATTERNS = [r"^# .+", r"^## .+", r"^## .+[\s\S]*^## .+"]
TICKET_PATTERNS = [r"(?i)what to build", r"(?im)^\W*blocked by", r"(?m)^- \[ \] "]
# Read like the Blocked-by line: any line that starts with "Track:".
_TRACK_LINE = re.compile(r"(?im)^\W*track:(.*)$")
_TICKET_NAME = re.compile(r"^(\d{2})-[a-z0-9][a-z0-9._-]*\.md$")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""
    count: int = 0   # tickets in a valid set
    # ticket number -> the track it names; only tickets that name one
    tracks: dict[int, str] = field(default_factory=dict)
    names: tuple[str, ...] = ()   # file names of a valid set, in ticket order


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


def _ticket_track(p: Path, allowed: Collection[str]) -> tuple[str, str]:
    """(the track the ticket's `Track: <name>` line names or "", "") — or
    ("", reason) when it has two such lines or names a track not in `allowed`."""
    number = p.name[:2]
    names = [m.strip(" \t*`") for m in _TRACK_LINE.findall(p.read_text(errors="replace"))]
    if len(names) > 1:
        return "", (f"ticket {number} carries {len(names)} Track: lines; a "
                    f"ticket names at most one track: {p}")
    if names and names[0] not in allowed:
        limit = (f"it must be one of {list(allowed)}" if allowed
                 else "no ticket of this task may name a track")
        return "", (f"ticket {number} names track {names[0]!r} on its Track: "
                    f"line; {limit}: {p}")
    return (names[0] if names else ""), ""


def check_tickets(tickets_dir: str | Path,
                  tracks: Collection[str] = ()) -> CheckResult:
    """A valid set: ≥1 ticket, numbers contiguous from 01 with no duplicates,
    every file carrying what-to-build, blocked-by and an unchecked criterion,
    and at most one `Track:` line, naming one of `tracks`."""
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
    named = {}
    for number, p in zip(numbers, files):
        r = _check(p, TICKET_PATTERNS, min_bytes=MIN_TICKET_BYTES)
        if not r.ok:
            return r
        track, reason = _ticket_track(p, tracks)
        if reason:
            return CheckResult(False, reason)
        if track:
            named[number] = track
    return CheckResult(True, count=len(files), tracks=named,
                       names=tuple(p.name for p in files))
