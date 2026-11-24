from datetime import datetime, timezone
from types import SimpleNamespace

from web.box_status import journal_runs


def _e(sec, msg):
    return SimpleNamespace(time=datetime.fromtimestamp(sec, timezone.utc),
                           unit="agent-ops-dispatcher.service", priority=6, message=msg)


def test_oneshot_starting_finished_makes_runs():
    runs = journal_runs([_e(0, "Starting x - d..."), _e(5, "Finished x - d."),
                         _e(10, "Starting x - d...")])["agent-ops-dispatcher"]
    assert [(r["duration_seconds"], r["result"]) for r in runs] == [(5, "success"), (None, None)]


def test_notify_starting_then_started_is_one_run():
    runs = journal_runs([_e(0, "Starting x"), _e(1, "Started x"),
                         _e(4, "Deactivated successfully.")])["agent-ops-dispatcher"]
    assert len(runs) == 1 and runs[0]["duration_seconds"] == 4
