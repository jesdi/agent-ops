"""dispatcher.deps: the pass's clock readings."""
from datetime import datetime, timezone

from dispatcher.deps import _cursor_now


def test_cursor_now_never_seals_the_second_it_was_taken_in():
    """GitHub stamps comments to the second, and pr_poll's trigger is a
    strict `>`. A cursor whose own second is already sealed hides every
    comment GitHub reports in that second — including ones posted after the
    session started. The cursor must be strictly below the second any
    comment written from now on can carry."""
    cursor = datetime.fromisoformat(_cursor_now())
    now_second = datetime.now(timezone.utc).replace(microsecond=0)
    assert cursor.microsecond == 0          # no sub-second precision
    assert cursor < now_second
