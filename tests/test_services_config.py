"""Acceptance tests for ticket 04: services config validate and render.

Black-box through `python -m dispatcher.services_config`. Interface defined here
(the ticket leaves it open):
- config / changes files are YAML maps keyed by job; config entries are
  `{schedule: {every: N} | {daily: "HH:MM", tz: ZONE} | {calendar: EXPR}, paused: bool}`
  and must hold all eight managed jobs (design.md data model).
- changes entries hold optional `paused` and optional `schedule` (same shape).
- managed job keys: dispatcher, update, onboard, keepalive, codex-keepalive,
  sweep, digest, triage; the timer of job J is `<unit-dir>/agent-ops-J.timer`.
- `render` prints JSON `{"enabled": [timer file names], "disabled": [...]}`.
- `systemd-analyze` is faked on PATH: it rejects any calendar naming "tuesday-ish".
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
JOBS = ["dispatcher", "update", "onboard", "keepalive", "codex-keepalive", "sweep", "digest", "triage"]


# What the page shows for each managed job: its name, and what it does.
ABOUT = {
    "dispatcher": ("Dispatcher pass", "Claims ready issues and drives the running tasks."),
    "update": ("Deploy sync", "Brings the box up to date with the latest agent-ops and infra config."),
    "onboard": ("Onboarding pass", "Moves the projects being onboarded to their next step."),
    "keepalive": ("Claude login keepalive", "Refreshes the Claude login before it expires."),
    "codex-keepalive": ("Codex login keepalive", "Refreshes the Codex login before it expires."),
    "sweep": ("Stale worktree sweep", "Removes the worktrees of tasks that have ended."),
    "digest": ("Daily digest", "Sends the daily summary of what the box did."),
    "triage": ("Backlog triage", "Scores and ranks the open issues of the backlog."),
}


def base_config() -> dict:
    cfg = {j: {"name": ABOUT[j][0], "description": ABOUT[j][1], "schedule": {"every": 5}, "paused": False}
           for j in JOBS}
    cfg["digest"]["schedule"] = {"daily": "08:00", "tz": "UTC"}
    return cfg


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "systemd-analyze"
    fake.write_text('#!/bin/sh\ncase "$*" in *tuesday-ish*) exit 1;; esac\nexit 0\n')
    fake.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}


def run(env, *args):
    return subprocess.run([sys.executable, "-m", "dispatcher.services_config", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
    return p


def validate(env, tmp_path, cfg):
    return run(env, "validate", str(write(tmp_path, "services.yaml", cfg)))


def render(env, tmp_path, cfg, changes=None):
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    r = run(env, "render", str(write(tmp_path, "services.yaml", cfg)),
            str(write(tmp_path, "changes.yaml", {} if changes is None else changes)), str(units))
    return r, units


def timer(units, job):
    return (units / f"agent-ops-{job}.timer").read_text()


@pytest.mark.parametrize("schedule", [
    {"every": 5},
    {"daily": "08:00", "tz": "UTC"},
    {"calendar": "Mon..Fri *-*-* 09:00"},
])
def test_valid_schedule_kinds_validate(env, tmp_path, schedule):
    cfg = base_config()
    cfg["digest"]["schedule"] = schedule
    r = validate(env, tmp_path, cfg)
    assert r.returncode == 0, r.stderr


def bad(mutate):
    cfg = base_config()
    mutate(cfg)
    return cfg


def _drop_paused(c):
    del c["digest"]["paused"]


REFUSED = {
    "every-zero": bad(lambda c: c["digest"].update(schedule={"every": 0})),
    "calendar-rejected": bad(lambda c: c["digest"].update(schedule={"calendar": "every tuesday-ish"})),
    "calendar-newline": bad(lambda c: c["digest"].update(
        schedule={"calendar": "*-*-* 09:00\nExecStart=/bin/true"})),
    "two-kinds": bad(lambda c: c["digest"].update(schedule={"every": 5, "calendar": "*-*-* 09:00"})),
    "no-paused": bad(_drop_paused),
    "no-name": bad(lambda c: c["digest"].pop("name")),
    "blank-name": bad(lambda c: c["digest"].update(name=" ")),
    "name-newline": bad(lambda c: c["digest"].update(name="Daily digest\nExecStart=/bin/true")),
    "no-description": bad(lambda c: c["digest"].pop("description")),
    "description-not-text": bad(lambda c: c["digest"].update(description=5)),
    "unknown-job": bad(lambda c: c.update(bogus={**c["digest"], "schedule": {"every": 5}})),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_invalid_config_is_refused_naming_the_job(env, tmp_path, case):
    r = validate(env, tmp_path, REFUSED[case])
    assert r.returncode != 0
    named = "bogus" if case == "unknown-job" else "digest"
    assert named in r.stdout + r.stderr


def test_daily_time_renders_at_that_time_in_utc(env, tmp_path):
    cfg = base_config()
    cfg["digest"]["schedule"] = {"daily": "09:00", "tz": "UTC"}
    r, units = render(env, tmp_path, cfg)
    assert r.returncode == 0, r.stderr
    assert re.search(r"^OnCalendar=\*-\*-\* 09:00(:00)? UTC$", timer(units, "digest"), re.M)


def test_every_ten_renders_a_ten_minute_timer(env, tmp_path):
    cfg = base_config()
    cfg["dispatcher"]["schedule"] = {"every": 10}
    r, units = render(env, tmp_path, cfg)
    assert r.returncode == 0, r.stderr
    assert re.search(r"^(OnCalendar=\*:0/10(:00)?|OnUnitActiveSec=10min)$", timer(units, "dispatcher"), re.M)


def report(r):
    out = json.loads(r.stdout)
    return set(out["enabled"]), set(out["disabled"])


def test_paused_config_reports_disabled_and_unpaused_enabled(env, tmp_path):
    cfg = base_config()
    cfg["triage"]["paused"] = True
    r, _ = render(env, tmp_path, cfg)
    assert r.returncode == 0, r.stderr
    enabled, disabled = report(r)
    assert "agent-ops-triage.timer" in disabled
    assert "agent-ops-dispatcher.timer" in enabled


def test_changes_pause_wins_over_config(env, tmp_path):
    r, _ = render(env, tmp_path, base_config(), {"digest": {"paused": True, "status": "recording"}})
    assert r.returncode == 0, r.stderr
    assert "agent-ops-digest.timer" in report(r)[1]


def test_changes_schedule_is_not_applied(env, tmp_path):
    r, units = render(env, tmp_path, base_config(),
                      {"dispatcher": {"schedule": {"every": 10}, "status": "recording"}})
    assert r.returncode == 0, r.stderr
    t = timer(units, "dispatcher")
    assert re.search(r"^(OnCalendar=\*:0/5(:00)?|OnUnitActiveSec=5min)$", t, re.M)
    assert "10" not in re.sub(r"\s", "", "".join(re.findall(r"^On\w+=.*$", t, re.M)))


def test_rendering_twice_is_byte_identical(env, tmp_path):
    cfg, changes = base_config(), {"digest": {"paused": True, "status": "recording"}}
    r1, units = render(env, tmp_path, cfg, changes)
    first = {p.name: p.read_bytes() for p in units.iterdir()}
    r2, _ = render(env, tmp_path, cfg, changes)
    assert r1.returncode == 0 and r2.returncode == 0, r1.stderr + r2.stderr
    assert first and first == {p.name: p.read_bytes() for p in units.iterdir()}
    assert r1.stdout == r2.stdout


def test_the_timer_is_described_by_the_job_name(env, tmp_path):
    r, units = render(env, tmp_path, base_config())
    assert r.returncode == 0, r.stderr
    assert re.search(r"^Description=Daily digest$", timer(units, "digest"), re.M)
