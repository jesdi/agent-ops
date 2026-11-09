"""Acceptance: `agent-ops status` prints a read-only report of the box from local
state alone. Driven through web.box_status.run (fake probes) and the CLI entry."""
import fcntl
import getpass
import io
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dispatcher import onboard_cli, state
from dispatcher.failures import write_quarantine
from dispatcher.state import PARK_CI, PARK_HUMAN, PARK_LOGIN, Stage
from dispatcher.usage import unavailable
from dispatcher.usage_providers import usage_to_json
from tests.usagefakes import session_usage
from tests.webfakes import make_task
from web import box_status
from web.box_status import Probes, Unit

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent
TIMER, SERVICE = "agent-ops-dispatcher.timer", "agent-ops-dispatcher.service"
BLOCKED = "VERDICT  not claiming: "
FREE = "VERDICT  no local blocker — candidates need GitHub (see console)"

TARGETS = """\
capacity: 3
state_dir: {state}
projects:
{projects}
"""
PROJECT = """\
  - name: {name}
    board: {{owner: jesdi, number: {n}}}
    paused: {paused}
    repositories:
      - name: {name}
        repo: jesdi/{name}
        github_id: {n}
        clone_path: /home/agent/repos/{name}
        worktrees_path: /home/agent/repos/{name}.worktrees
        gate_cmd: make gate
"""


class Box:
    """A state dir and targets.yaml under tmp_path, plus the fake probes."""

    def __init__(self, tmp_path, monkeypatch):
        self.state = tmp_path / "state"
        self.state.mkdir()
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(self.state))
        self.config = tmp_path / "targets.yaml"
        self.units = {TIMER: Unit("active", NOW - timedelta(days=2)),
                      SERVICE: Unit("inactive", NOW - timedelta(minutes=3))}
        self.tabs = set()
        self.calls = []
        self.projects(agent_ops=False, billing=False)
        self.pass_ago(3)
        self.usage(session_usage(0.5, now=NOW))

    def projects(self, agent_ops, billing):
        body = "".join(PROJECT.format(name=n, n=i, paused=str(p).lower())
                       for i, (n, p) in enumerate(
                           [("agent-ops", agent_ops), ("billing", billing)], 1))
        self.config.write_text(TARGETS.format(state=self.state, projects=body))

    def pass_ago(self, minutes, interval=5):
        self.write("pass.json", {
            "started_at": (NOW - timedelta(minutes=minutes + 1)).isoformat(),
            "finished_at": (NOW - timedelta(minutes=minutes)).isoformat(),
            "interval_minutes": interval})

    def write(self, name, doc):
        p = self.state / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc))

    def usage(self, reading, age_s=120):
        at = NOW.timestamp() - age_s
        self.write("usage/anthropic.json", {
            "fetched_at": at, "usage": usage_to_json(replace(reading, fetched_at=at))})

    def dark(self):
        self.usage(unavailable("anthropic", NOW.timestamp() - 120))

    def task(self, issue, repository="agent-ops", **kw):
        kw.setdefault("updated_at", "2026-11-06T10:00:00+00:00")
        state.save(self.state, make_task(issue=issue, repository=repository, **kw))

    def probes(self):
        def units():
            self.calls.append("units")
            return dict(self.units)

        def tabs():
            self.calls.append("tabs")
            return set(self.tabs)
        return Probes(units=units, tabs=tabs)

    def report(self):
        out = io.StringIO()
        code = box_status.run(str(self.config), NOW, self.probes(), out)
        return code, out.getvalue()


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def verdict(text):
    return text.splitlines()[0]


def row(text, label):
    """The report line of row `label` (its first word)."""
    lines = [l for l in text.splitlines()[1:] if l.split(None, 1)[:1] == [label]]
    assert len(lines) == 1, f"no single {label!r} row in:\n{text}"
    return lines[0]


def inflight(box, n, **kw):
    for i in range(1, n + 1):
        box.task(i, **kw)


def test_both_projects_paused_is_projects_paused(box):
    box.projects(agent_ops=True, billing=True)
    code, text = box.report()
    assert verdict(text) == (BLOCKED + "projects-paused "
                             "(agent-ops, billing paused in targets.yaml)")
    assert "agent-ops: PAUSED · billing: PAUSED" in row(text, "projects")


def test_one_paused_project_is_not_a_blocker(box):
    box.projects(agent_ops=False, billing=True)
    code, text = box.report()
    assert verdict(text) == FREE
    assert "agent-ops: active · billing: PAUSED" in row(text, "projects")


def test_stale_heartbeat_is_dispatcher_down(box):
    box.pass_ago(11)
    assert verdict(box.report()[1]) == (
        BLOCKED + "dispatcher-down (last pass 11m ago, interval 5m)")


def test_missing_heartbeat_is_dispatcher_down(box):
    (box.state / "pass.json").unlink()
    assert verdict(box.report()[1]) == BLOCKED + "dispatcher-down (no pass.json)"


def test_heartbeat_at_exactly_twice_the_interval_is_fresh(box):
    box.pass_ago(10)
    assert "dispatcher-down" not in verdict(box.report()[1])


def test_failed_dispatcher_timer_is_dispatcher_down(box):
    box.units[TIMER] = Unit("failed", NOW - timedelta(minutes=30))
    code, text = box.report()
    assert verdict(text) == (
        BLOCKED + "dispatcher-down (agent-ops-dispatcher.timer failed)")
    units = row(text, "units")
    assert TIMER in units and "failed" in units and "11:30" in units


def test_stale_pass_while_the_service_runs_is_busy_not_down(box):
    box.pass_ago(11)
    box.units[SERVICE] = Unit("active", NOW - timedelta(minutes=8))
    line = verdict(box.report()[1])
    assert "dispatcher busy (pass running since 11:52)" in line
    assert "dispatcher-down" not in line


def test_triage_hold_beats_budget_and_paused_projects(box):
    box.write("triage-request.json",
              {"requested_at": (NOW - timedelta(minutes=14)).isoformat()})
    box.usage(session_usage(0.95, 40, now=NOW))
    box.projects(agent_ops=True, billing=True)
    code, text = box.report()
    assert verdict(text) == (
        BLOCKED + "claims-paused (triage-request.json, 14m old)")
    assert "triage" in row(text, "holds")


def test_dead_login_with_marker_is_auth_dark(box):
    box.dark()
    box.write("auth-dark", {"since": "2026-11-06T11:20:00+00:00", "alerted": False})
    code, text = box.report()
    assert verdict(text) == (BLOCKED + "auth-dark "
                             "(usage unknowable since 11:20 — re-login likely)")
    assert "auth-dark since 11:20" in row(text, "holds")


def test_dead_login_without_marker_is_still_auth_dark(box):
    box.dark()
    assert verdict(box.report()[1]) == BLOCKED + "auth-dark (usage unknowable)"


def test_marker_alone_never_makes_a_verdict(box):
    box.write("auth-dark", {"since": "2026-11-06T11:20:00+00:00", "alerted": False})
    assert verdict(box.report()[1]) == FREE


def test_spent_budget_is_budget_blocked(box):
    box.usage(session_usage(0.95, 40, now=NOW))
    assert verdict(box.report()[1]) == (
        BLOCKED + "budget-blocked (resets in 40m, usage read 2m ago)")


def test_three_unparked_tasks_fill_capacity(box):
    inflight(box, 3)
    code, text = box.report()
    assert verdict(text) == BLOCKED + "capacity-full (3/3 slots)"
    assert "3/3" in row(text, "slots")


def test_parked_tasks_free_their_slot(box):
    box.task(1)
    box.task(2, park=PARK_HUMAN)
    box.task(3, park=PARK_HUMAN)
    code, text = box.report()
    assert "capacity-full" not in verdict(text)
    assert "1/3" in row(text, "slots")


def test_parked_login_keeps_its_slot(box):
    box.task(1)
    box.task(2)
    box.task(3, park=PARK_LOGIN)
    assert verdict(box.report()[1]) == BLOCKED + "capacity-full (3/3 slots)"


def test_parked_login_task_keeps_its_live_tab_name(box):
    box.task(284, park=PARK_LOGIN)
    box.tabs = {"task-agent-ops-284"}
    assert "agent-ops#284  implement  parked-login  task-agent-ops-284  2h" in box.report()[1]


def test_quarantine_record_is_listed_without_any_network_call(box, monkeypatch):
    import subprocess
    import urllib.request
    for target, name in ((subprocess, "run"), (subprocess, "Popen"),
                         (urllib.request, "urlopen")):
        monkeypatch.setattr(target, name, lambda *a, **k: pytest.fail("outside call"))
    write_quarantine(box.state, "agent-ops", 212, "agent-ops", 205, "fp")
    assert "quarantine: agent-ops#212 blocked by agent-ops#205" in row(
        box.report()[1], "holds")


def snapshot(root):
    return {str(p): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def test_report_changes_nothing_and_takes_no_lock(box, monkeypatch):
    box.projects(agent_ops=True, billing=True)
    box.task(270, stage=Stage.REVIEW, park=PARK_CI)
    box.task(281)
    box.tabs = {"task-agent-ops-281"}
    before = snapshot(box.state)

    def locked(*a, **k):
        pytest.fail("took a lock")
    monkeypatch.setattr(fcntl, "flock", locked)
    monkeypatch.setattr(fcntl, "lockf", locked)
    code, text = box.report()
    assert text, "no report printed"
    assert snapshot(box.state) == before
    assert not (box.state / "convergence.lock").exists()
    assert set(box.calls) <= {"units", "tabs"}


def test_exit_code_is_zero_even_when_the_dispatcher_is_down(box):
    box.pass_ago(11)
    code, text = box.report()
    assert verdict(text).startswith(BLOCKED + "dispatcher-down")
    assert code == 0


def test_cli_refuses_any_user_but_agent(box, monkeypatch, capsys):
    monkeypatch.setattr(getpass, "getuser", lambda: "root")
    try:
        code = onboard_cli.main(["status", "--config", str(box.config)])
    except SystemExit as e:
        code = e.code
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).strip().splitlines()
    assert code == 2
    assert len(lines) == 1 and "agent" in lines[0] and "usage:" not in lines[0]


def test_claude_md_only_imports_agents_md():
    assert (ROOT / "CLAUDE.md").read_text().strip() == "@AGENTS.md"


def test_agents_md_points_agents_at_the_status_command():
    text = (ROOT / "AGENTS.md").read_text()
    needles = ["ssh box '~/.local/bin/agent-ops status'", "/diagnosing-bugs",
               "agent@ubuntu-4gb-hel1-1", "~/agent-ops-state", "targets.yaml",
               "pass.json", "task-*.json", "quarantine/", "triage-request.json",
               "budget-stalled", "auth-dark", "systemctl --user",
               "task-<repo>-<issue>", "agent-ops-herdr.service"]
    assert [n for n in needles if n not in text] == []
    assert "kills every live session" in text
