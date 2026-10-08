import pytest
from pathlib import Path

from dispatcher.artifacts import check_spec, check_tickets, ticket_files

GOOD_SPEC = (
    "# widget frobnicator — design\n\n"
    "## Problem\n\n" + ("Detail about the problem. " * 30) + "\n\n"
    "## Decisions\n\n" + ("A decision and its rationale. " * 30) + "\n"
)


def test_spec_ok(tmp_path: Path):
    p = tmp_path / "spec.md"
    p.write_text(GOOD_SPEC)
    assert check_spec(p).ok


def test_spec_missing(tmp_path: Path):
    r = check_spec(tmp_path / "nope.md")
    assert not r.ok and "missing" in r.reason


def test_spec_too_small(tmp_path: Path):
    p = tmp_path / "spec.md"
    p.write_text("# tiny\n\n## Problem\n\n## Decisions\n")
    r = check_spec(p)
    assert not r.ok and "small" in r.reason


def test_spec_missing_headings(tmp_path: Path):
    p = tmp_path / "spec.md"
    p.write_text("x" * 5000)
    r = check_spec(p)
    assert not r.ok and "heading" in r.reason


def ticket(n: int, slug: str = "thing", what="**What to build:** ",
           blocked="**Blocked by:** None — can start immediately",
           crit="- [ ] The thing is observable end to end") -> tuple[str, str]:
    body = (f"# {n:02d} — {slug}\n\n{what}" + ("behaviour " * 30) + "\n\n"
            f"{blocked}\n\n**Status:** ready-for-agent\n\n{crit}\n")
    return f"{n:02d}-{slug}.md", body


def tickets_dir(tmp_path: Path, *specs) -> Path:
    d = tmp_path / ".agent" / "tickets"
    d.mkdir(parents=True)
    for name, body in specs:
        (d / name).write_text(body)
    return d


def test_tickets_ok_counts_them(tmp_path: Path):
    d = tickets_dir(tmp_path, ticket(1), ticket(2, "other"))
    r = check_tickets(d)
    assert r.ok and r.count == 2


def test_ticket_files_sorted_numerically_and_ignores_strays(tmp_path: Path):
    d = tickets_dir(tmp_path, ticket(10, "ten"), ticket(2, "two"), ("README.md", "x"))
    assert [p.name for p in ticket_files(d)] == ["02-two.md", "10-ten.md"]


def test_tickets_dir_missing(tmp_path: Path):
    r = check_tickets(tmp_path / ".agent" / "tickets")
    assert not r.ok and "missing" in r.reason


def test_tickets_dir_empty(tmp_path: Path):
    d = tickets_dir(tmp_path, ("notes.md", "x"))
    r = check_tickets(d)
    assert not r.ok and "no ticket" in r.reason


@pytest.mark.parametrize("field", ["what", "blocked", "crit"])
def test_ticket_missing_section_fails(tmp_path: Path, field):
    d = tickets_dir(tmp_path, ticket(1, **{field: "nothing here"}))
    r = check_tickets(d)
    assert not r.ok and "01-thing.md" in r.reason


def test_ticket_too_small(tmp_path: Path):
    d = tickets_dir(tmp_path, ("01-tiny.md", "# 01\n**What to build:** x\n**Blocked by:** none\n- [ ] y\n"))
    r = check_tickets(d)
    assert not r.ok and "small" in r.reason


def test_ticket_numbering_gap_fails(tmp_path: Path):
    d = tickets_dir(tmp_path, ticket(1), ticket(3))
    r = check_tickets(d)
    assert not r.ok and "contiguous" in r.reason


def test_ticket_numbering_must_start_at_one(tmp_path: Path):
    d = tickets_dir(tmp_path, ticket(2))
    assert not check_tickets(d).ok


def test_ticket_duplicate_number_fails(tmp_path: Path):
    d = tickets_dir(tmp_path, ticket(1, "a"), ticket(1, "b"))
    r = check_tickets(d)
    assert not r.ok and "duplicate" in r.reason


# --- ticket tracks (specs/pinned-tracks, requirement 8) ----------------------

def tracked(n: int, *lines: str) -> tuple[str, str]:
    name, body = ticket(n, f"t{n}")
    return name, body + "".join(f"\n{line}\n" for line in lines)


def test_tickets_return_the_tracks_they_name(tmp_path: Path):
    d = tickets_dir(tmp_path, tracked(1), tracked(2, "Track: frontend"),
                    tracked(3, "**Track:** `architecture`"))
    r = check_tickets(d, ("architecture", "frontend"))
    assert r.ok and r.count == 3
    assert r.tracks == {2: "frontend", 3: "architecture"}


def test_tickets_without_a_track_line_name_none(tmp_path: Path):
    r = check_tickets(tickets_dir(tmp_path, tracked(1)), ("frontend",))
    assert r.ok and r.tracks == {}


def test_a_ticket_that_names_a_track_it_may_not_use_fails(tmp_path: Path):
    d = tickets_dir(tmp_path, tracked(1), tracked(2, "Track: standard"))
    r = check_tickets(d, ("architecture", "frontend"))
    assert not r.ok and not r.tracks
    assert "ticket 02" in r.reason and "'standard'" in r.reason
    assert "architecture" in r.reason and "frontend" in r.reason


def test_a_ticket_with_two_track_lines_fails(tmp_path: Path):
    d = tickets_dir(tmp_path, tracked(1, "Track: frontend", "Track: frontend"))
    r = check_tickets(d, ("frontend",))
    assert not r.ok and "ticket 01" in r.reason and "2 Track: lines" in r.reason


def test_no_ticket_may_name_a_track_when_none_is_usable(tmp_path: Path):
    d = tickets_dir(tmp_path, tracked(1, "Track: frontend"))
    r = check_tickets(d)
    assert not r.ok and "ticket 01" in r.reason
    assert "no ticket of this task may name a track" in r.reason


def test_an_empty_track_line_fails(tmp_path: Path):
    d = tickets_dir(tmp_path, tracked(1, "Track:"))
    assert not check_tickets(d, ("frontend",)).ok
