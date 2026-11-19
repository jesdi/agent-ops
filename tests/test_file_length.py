from scripts.file_length import ALLOWED, EXEMPT, MAX_LINES, tracked_lengths, violations


def test_a_file_over_the_limit_fails_unless_exempt_or_allowed():
    allowed = next(iter(ALLOWED))
    lengths = {"a.py": MAX_LINES + 1, "b.py": MAX_LINES,
               next(iter(EXEMPT)): MAX_LINES * 3}
    lengths |= {path: MAX_LINES + 1 for path in ALLOWED}

    assert violations(lengths) == [f"a.py: {MAX_LINES + 1} lines (max {MAX_LINES})"]

    lengths[allowed] = MAX_LINES
    assert violations(lengths)[-1].startswith(f"{allowed}: in ALLOWED but no longer over")


def test_no_tracked_source_file_is_over_the_limit():
    assert violations(tracked_lengths()) == []
