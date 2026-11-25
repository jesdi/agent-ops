"""Dispatcher health and the next-pass countdown, apart: a calendar schedule shows no
countdown, yet a heartbeat two of its gaps old still reads as a stopped dispatcher."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

import dispatcher.main as main
from dispatcher import services_config
from tests.dispatchfakes import deps, patch_usage
from tests.test_services_config import base_config
from tests.test_upkeep import cfg
from web.read_model import PassClock, pass_clock

FINISHED = "2026-11-24T10:00:00+00:00"
DUE = "2026-11-25T09:00:00+00:00"  # 23 h later


def at(hour, day=25):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def test_a_calendar_heartbeat_within_two_gaps_is_alive_with_no_countdown():
    hb = {"finished_at": FINISHED, "custom": True, "next_at": DUE}
    assert pass_clock(hb, at(9)) == PassClock(True, "")
    assert pass_clock(hb, at(8, day=26)) == PassClock(True, "")


def test_a_calendar_heartbeat_two_gaps_old_is_dead():
    hb = {"finished_at": FINISHED, "custom": True, "next_at": DUE}
    assert pass_clock(hb, at(9, day=26)) == PassClock(False, "")


def test_a_calendar_heartbeat_without_a_due_time_is_alive_for_two_days():
    hb = {"finished_at": FINISHED, "custom": True}
    assert pass_clock(hb, at(9, day=26)).alive
    assert not pass_clock(hb, at(11, day=26)).alive
    assert not pass_clock({**hb, "next_at": "garbage"}, at(9, day=30)).alive
    assert not pass_clock({"custom": True}, at(9)).alive


def test_a_calendar_pass_records_when_the_next_is_due(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    fake = tmp_path / "bin" / "systemd-analyze"  # the calendar validates anywhere
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    c = cfg(tmp_path)
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    config = base_config()
    config["dispatcher"]["schedule"] = {"calendar": "Mon..Fri *-*-* 09:00"}
    (Path(c.state_dir) / "services.yaml").write_text(yaml.safe_dump(config))
    walked = []
    monkeypatch.setattr(services_config, "calendar_runs", lambda cal, n: walked.append((cal, n)) or [DUE])

    main.run_pass(c, deps())

    hb = json.loads((Path(c.state_dir) / "pass.json").read_text())
    assert (hb["custom"], hb["next_at"]) == (True, DUE)
    assert walked == [("Mon..Fri *-*-* 09:00", 1)]
