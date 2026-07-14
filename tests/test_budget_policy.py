from dispatcher.budget import UsageSnapshot, should_spawn

KW = dict(threshold=0.8, racing_minutes=30, racing_threshold=0.95)


def snap(util, mins, source="oauth"):
    return UsageSnapshot(utilization=util, minutes_to_reset=mins, source=source)


def test_spawn_when_under_threshold():
    assert should_spawn(snap(0.5, 200), **KW)


def test_no_spawn_when_over_threshold():
    assert not should_spawn(snap(0.85, 200), **KW)


def test_reset_racing_relaxes_threshold():
    # window resets soon → spawn eagerly so the remainder isn't wasted
    assert should_spawn(snap(0.9, 20), **KW)


def test_reset_racing_still_bounded():
    assert not should_spawn(snap(0.97, 20), **KW)


def test_unavailable_source_is_conservative():
    # both usage sources dark → no new spawns
    assert not should_spawn(snap(0.1, 200, source="unavailable"), **KW)


def test_ccusage_fallback_counts():
    assert should_spawn(snap(0.5, 200, source="ccusage"), **KW)
