from datetime import datetime, timezone

from web.box_status import parse_journal, parse_units

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
