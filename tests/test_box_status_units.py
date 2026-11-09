import dataclasses
import io
import subprocess
from datetime import datetime, timezone

from datetime import timedelta

import pytest

from dispatcher.state import PARK_HUMAN, WHY_STALLED
from tests.test_box_status_acceptance import NOW, SERVICE, Box, row, verdict
from web import box_status
from web.box_status import Unit, parse_journal, parse_units, system_probes

SHOW = """Id=agent-ops-dispatcher.timer
ActiveState=active
StateChangeTimestamp=@1762700000

Id=agent-ops-web.service
ActiveState=failed
StateChangeTimestamp=@1762703600

Id=agent-ops-never.service
ActiveState=inactive
StateChangeTimestamp=
"""


def test_parse_units_reads_each_block_and_skips_units_without_a_stamp():
    units = parse_units(SHOW)

    assert set(units) == {"agent-ops-dispatcher.timer", "agent-ops-web.service"}
    timer, web = units["agent-ops-dispatcher.timer"], units["agent-ops-web.service"]
    assert timer.state == "active"
    assert timer.since == datetime.fromtimestamp(1762700000, timezone.utc)
    assert web.state == "failed"
    assert web.since == datetime.fromtimestamp(1762703600, timezone.utc)


def test_parse_units_of_empty_output_is_empty():
    assert parse_units("") == {}


def test_parse_journal_reads_json_lines_and_skips_malformed_ones():
    good = ('{"__REALTIME_TIMESTAMP":"1762700000000000","_SYSTEMD_USER_UNIT":'
            '"agent-ops-web.service","PRIORITY":"3","MESSAGE":"boom"}')
    entries = parse_journal(f"{good}\nnot json\n{{\"MESSAGE\":\"no stamp\"}}\n")

    assert len(entries) == 1
    assert (entries[0].unit, entries[0].priority, entries[0].message) == (
        "agent-ops-web.service", 3, "boom")


def test_a_real_journalctl_failure_prints_its_stderr_once(tmp_path, monkeypatch):
    monkeypatch.setattr(box_status.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, "", "Permission denied\n"))
    box = Box(tmp_path, monkeypatch)
    out = io.StringIO()
    probes = dataclasses.replace(box.probes(), journal=box_status._journal)
    box_status.run(str(box.config), NOW, probes, out)

    assert row(out.getvalue(), "errors").split(None, 1)[1] == (
        "unknown (journalctl failed: Permission denied)")


def test_a_oneshot_pass_in_activating_is_busy_not_down(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch)
    box.pass_ago(11)
    box.units[SERVICE] = Unit("activating", NOW - timedelta(minutes=8))
    assert "dispatcher busy (pass running since 11:52)" in verdict(box.report()[1])


@pytest.mark.parametrize("body, why", [
    ("{torn", "unknown (unreadable pass.json)"),
    ('{"finished_at": "2026-11-06T11:57:00", "interval_minutes": 5}',
     "unknown (pass.json finished_at missing or naive)"),
])
def test_an_unusable_pass_json_is_not_reported_as_missing(tmp_path, monkeypatch, body, why):
    box = Box(tmp_path, monkeypatch)
    (box.state / "pass.json").write_text(body)
    text = box.report()[1]
    assert verdict(text) == f"VERDICT  not claiming: dispatcher-down ({why})"
    assert row(text, "dispatcher").split(None, 1)[1].startswith(why)


def test_a_human_park_shows_its_reason_after_a_slash(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch)
    box.task(55, park=PARK_HUMAN, park_reason=WHY_STALLED)
    assert f"agent-ops#55  implement  {PARK_HUMAN}/{WHY_STALLED}" in box.report()[1]


def test_an_unparked_task_without_its_tab_has_no_session(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch)
    box.task(281)
    assert "agent-ops#281  implement  no session  2h" in box.report()[1]


def test_system_probes_only_query(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(box_status.subprocess, "run", run)
    probes = system_probes()
    probes.units()
    probes.journal()

    assert [argv[:3] for argv in calls] == [
        ["systemctl", "--user", "show"], ["journalctl", "--user", "-u"]]
