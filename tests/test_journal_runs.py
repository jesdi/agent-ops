from datetime import datetime, timezone
from types import SimpleNamespace

from web.box_status import journal_runs, service_runs


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


def test_an_operator_stop_is_recorded_as_stopped_not_success():
    runs = journal_runs([_e(0, "Starting x - d..."), _e(3, "Stopping x - d..."),
                         _e(4, "x.service: Deactivated successfully."),
                         _e(4, "Stopped x - d.")])["agent-ops-dispatcher"]
    assert [(r["duration_seconds"], r["result"]) for r in runs] == [(4, "stopped")]


def test_a_stopping_run_stays_open_until_its_end_line():
    entries = [_e(0, "Starting x - d..."), _e(3, "Stopping x - d...")]
    assert journal_runs(entries)["agent-ops-dispatcher"][0]["result"] is None
    runs = journal_runs(entries + [_e(40, "x.service: Deactivated successfully."),
                                   _e(40, "Stopped x - d.")])["agent-ops-dispatcher"]
    assert [(r["duration_seconds"], r["result"]) for r in runs] == [(40, "stopped")]


def test_a_stop_does_not_leak_into_the_next_run():
    runs = journal_runs([_e(0, "Starting x"), _e(1, "Stopping x"), _e(2, "Stopped x"),
                         _e(5, "Starting x"), _e(9, "Deactivated successfully.")]
                        )["agent-ops-dispatcher"]
    assert [r["result"] for r in runs] == ["stopped", "success"]


def test_lines_of_other_units_are_ignored():
    timer = SimpleNamespace(time=datetime.fromtimestamp(0, timezone.utc),
                            unit="agent-ops-dispatcher.timer", priority=6, message="Started x timer.")
    assert journal_runs([timer]) == {}


def test_output_of_a_stopping_run_keeps_it_open_and_stopping():
    runs = journal_runs([_e(0, "Starting x"), _e(1, "Stopping x"), _e(2, "draining 3 tasks"),
                         _e(6, "Deactivated successfully.")])["agent-ops-dispatcher"]
    assert [(r["duration_seconds"], r["result"]) for r in runs] == [(6, "stopped")]


def test_a_job_with_fewer_than_five_runs_in_a_week_reads_its_older_journal():
    def run(day, unit):
        return [SimpleNamespace(time=datetime(2026, 11, day, 9, tzinfo=timezone.utc), unit=unit,
                                priority=6, message="Starting x..."),
                SimpleNamespace(time=datetime(2026, 11, day, 9, 1, tzinfo=timezone.utc), unit=unit,
                                priority=6, message="Finished x.")]
    weekly = [e for day in (1, 8, 15, 22) for e in run(day, "agent-ops-digest.service")]
    calls = []

    def journal(since, unit="agent-ops-*", lines=None):
        calls.append((since, unit))
        return weekly[-2:] if since else (weekly if unit == "agent-ops-digest.service" else [])

    runs = service_runs(journal)

    assert len(runs["agent-ops-digest"]) == 4
    assert "agent-ops-sweep" not in runs  # a job with no runs at all stays absent
    assert ("7 days ago", "agent-ops-*") in calls
