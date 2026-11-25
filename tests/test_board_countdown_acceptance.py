"""Acceptance (services-section ticket 12): the board's next-pass countdown follows
the dispatcher pass's schedule in the installed services config.

Black-box: a real `run_pass` writes its heartbeat, then `GET /api/board` is read
through TestClient on a real `Sources`. Interface defined here (the ticket leaves
it open): the installed services config is `<state_dir>/services.yaml`, a YAML map
keyed by managed job, each `{schedule: {every: N} | {daily, tz} | {calendar}, paused}`
(the format of dispatcher/services_config.py). A missing file means no installed
config. `systemd-analyze` is faked on PATH so a custom expression validates anywhere.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import dispatcher.main as main
from dispatcher.config import load_config
from tests.dispatchfakes import deps, freeze_clock, patch_usage
from tests.webfakes import HEADERS
from web.app import create_app
from web.sources import Sources

FINISHED = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
JOBS = ["dispatcher", "update", "onboard", "keepalive", "codex-keepalive", "sweep", "digest", "triage"]

TARGETS = """\
state_dir: {state}
{extra}
projects:
  - name: portfolio-eval
    board: {{owner: jesdi, number: 1}}
    repositories:
      - name: portfolio_eval
        repo: jesdi/portfolio_eval
        github_id: 1
        clone_path: /tmp/c
        worktrees_path: /tmp/w
        setup_cmd: setup
        verify_cmd: "make e2e-slot SLOT={{slot}}"
        gate_cmd: "make gate-slot SLOT={{slot}}"
"""


@pytest.fixture(autouse=True)
def fake_systemd_analyze(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "systemd-analyze"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def install(tmp_path, dispatcher_schedule):
    config = {j: {"schedule": {"every": 5}, "paused": False} for j in JOBS}
    config["dispatcher"]["schedule"] = dispatcher_schedule
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "services.yaml").write_text(yaml.safe_dump(config))


def board_after_a_pass(tmp_path, monkeypatch, extra=""):
    """Run one dispatcher pass finishing at 10:00 UTC, then read the board at 10:01."""
    p = tmp_path / "targets.yaml"
    p.write_text(TARGETS.format(state=tmp_path / "state", extra=extra))
    c = load_config(p)
    patch_usage(monkeypatch)
    freeze_clock(monkeypatch, FINISHED)
    main.run_pass(c, deps())
    now = FINISHED.replace(minute=1)
    client = TestClient(create_app(c, Sources(c.state_dir, None, None), clock=lambda: now))
    r = client.get("/api/board", headers=HEADERS)
    assert r.status_code == 200
    return r.json()["next_claim"]


def eta(next_claim):
    return datetime.fromisoformat(next_claim["next_pass_eta"])


def test_countdown_follows_an_installed_every_10_minutes(tmp_path, monkeypatch):
    install(tmp_path, {"every": 10})
    nc = board_after_a_pass(tmp_path, monkeypatch)
    assert eta(nc) == datetime(2026, 11, 24, 10, 10, tzinfo=timezone.utc)


def test_installed_config_wins_over_targets_pass_interval(tmp_path, monkeypatch):
    install(tmp_path, {"every": 10})
    nc = board_after_a_pass(tmp_path, monkeypatch, extra="pass_interval_minutes: 5")
    assert eta(nc) == datetime(2026, 11, 24, 10, 10, tzinfo=timezone.utc)


def test_no_installed_config_keeps_the_5_minute_countdown(tmp_path, monkeypatch):
    # pass_interval_minutes is no longer used, so a stale value there changes nothing
    nc = board_after_a_pass(tmp_path, monkeypatch, extra="pass_interval_minutes: 7")
    assert eta(nc) == datetime(2026, 11, 24, 10, 5, tzinfo=timezone.utc)


def test_custom_schedule_shows_no_countdown_and_is_not_reported_down(tmp_path, monkeypatch):
    install(tmp_path, {"calendar": "Mon..Fri *-*-* 09:00"})
    nc = board_after_a_pass(tmp_path, monkeypatch)
    assert nc["next_pass_eta"] == ""
    assert nc["verdict"] != "unknown"
