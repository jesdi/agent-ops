"""Acceptance (ticket 04): the `errors` row of `agent-ops status`, fed by a fake
journal probe.

Interface defined here: `Probes.journal` is a zero-argument callable returning the
`agent-ops` journal entries as objects with `.time` (aware datetime), `.unit`
(e.g. `agent-ops-web.service`), `.priority` (syslog 0-7, lower is more severe) and
`.message`, oldest first. It may return entries the report must filter out (old,
mild, foreign unit). When journalctl fails it raises with the stderr as message."""
import io
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_box_status_acceptance import NOW, Box, row, verdict
from web import box_status
from web.box_status import Probes

WEB, DISPATCHER = "agent-ops-web.service", "agent-ops-dispatcher.service"
ERR, WARNING, NOTICE, INFO = 3, 4, 5, 6
ROWS = ["dispatcher", "units", "projects", "holds", "slots", "tasks", "questions"]


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def entry(ago, message, priority=INFO, unit=WEB):
    return SimpleNamespace(time=NOW - ago, unit=unit, priority=priority, message=message)


def report(box, journal):
    base = box.probes()
    out = io.StringIO()
    probes = Probes(units=base.units, tabs=base.tabs, journal=journal)
    code = box_status.run(str(box.config), NOW, probes, out)
    return code, out.getvalue()


def section(text, label):
    """The row `label` plus its indented continuation lines."""
    lines = text.splitlines()[1:]
    for i, l in enumerate(lines):
        if l.split(None, 1)[:1] == [label]:
            found = [l]
            for more in lines[i + 1:]:
                if not more.startswith(" "):
                    break
                found.append(more)
            return "\n".join(found)
    pytest.fail(f"no {label!r} row in:\n{text}")


def test_errors_row_shows_last_20_matching_lines_with_time_and_unit(box):
    minutes = lambda k: timedelta(minutes=(26 - k) * 10)   # m01 oldest .. m25 at 11:50
    entries = [entry(timedelta(hours=7), "stale-err", ERR)]
    for k in range(1, 26):
        unit = WEB if k % 2 else DISPATCHER
        if k % 3:
            entries.append(entry(minutes(k), f"boom m{k:02d}", ERR, unit))
        else:
            entries.append(entry(minutes(k), f"Traceback (most recent call last) m{k:02d}",
                                 INFO, unit))
    entries.append(entry(timedelta(minutes=5), "claimed agent-ops#290", INFO))
    entries.sort(key=lambda e: e.time)
    code, text = report(box, lambda: entries)
    errors = section(text, "errors")
    shown = [l for l in errors.splitlines() if re.search(r"m\d\d\b", l)]
    assert [re.search(r"m(\d\d)\b", l).group(1) for l in shown] == [
        f"{k:02d}" for k in range(6, 26)]
    for l in shown:
        k = int(re.search(r"m(\d\d)\b", l).group(1))
        assert (NOW - minutes(k)).strftime("%H:%M") in l
        assert (WEB if k % 2 else DISPATCHER) in l
    assert "claimed agent-ops#290" not in errors
    assert "stale-err" not in errors
    assert code == 0


def test_a_long_line_is_cut_to_200_characters(box):
    entries = [entry(timedelta(minutes=5), "long " + "x" * 300, ERR)]
    code, text = report(box, lambda: entries)
    runs = [len(r) for r in re.findall(r"x+", section(text, "errors"))]
    assert runs and 150 <= max(runs) <= 200


def test_foreign_units_and_mild_priorities_are_left_out(box):
    entries = [
        entry(timedelta(minutes=9), "foreign-boom", ERR, "ssh.service"),
        entry(timedelta(minutes=8), "notice-line", NOTICE),
        entry(timedelta(minutes=7), "warning-line", WARNING),
        entry(timedelta(minutes=6), "plain-info", INFO),
        entry(timedelta(minutes=5), "Failed with result 'exit-code'", INFO, DISPATCHER),
    ]
    code, text = report(box, lambda: entries)
    errors = section(text, "errors")
    assert "warning-line" in errors and "Failed with result" in errors
    for gone in ("foreign-boom", "notice-line", "plain-info"):
        assert gone not in errors


def test_no_matching_line_reads_none(box):
    code, text = report(box, lambda: [entry(timedelta(minutes=5), "claimed agent-ops#290")])
    assert row(text, "errors").split(None, 1)[1:] == ["none"]


def test_journalctl_failure_degrades_only_the_errors_row(box):
    def journal():
        raise RuntimeError("Failed to connect to bus: No medium found")
    code, text = report(box, journal)
    assert row(text, "errors").split(None, 1)[1] == (
        "unknown (journalctl failed: Failed to connect to bus: No medium found)")
    assert verdict(text).startswith("VERDICT  ")
    for label in ROWS:
        row(text, label)
    assert code == 0
