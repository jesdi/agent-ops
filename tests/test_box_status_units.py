from datetime import datetime, timezone

from web.box_status import parse_units

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
