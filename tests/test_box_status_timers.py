"""systemd_timers against canned systemctl output: an installed-but-unloaded
timer still lists, and a monotonic (OnUnitActiveSec) timer gets a schedule
and a next run."""
import subprocess
from types import SimpleNamespace

from dispatcher import services_config
from web import box_status
from web.services import describe_schedule

LIST = "agent-ops-dispatcher.timer enabled enabled\nagent-ops-triage.timer disabled enabled\nfoo@.timer static -\n"
TIMERS = """Id=agent-ops-dispatcher.timer
Description=dispatcher
ActiveState=active
FragmentPath=/home/agent/.config/systemd/user/agent-ops-dispatcher.timer
TimersCalendar=
TimersMonotonic={ OnUnitActiveUSec=5min ; next_elapse=1h }
TimersMonotonic={ OnBootUSec=1min ; next_elapse=1min }
NextElapseUSecRealtime=
LastTriggerUSec=@1795000080

Id=agent-ops-triage.timer
Description=triage
ActiveState=inactive
FragmentPath=/home/agent/.config/systemd/user/agent-ops-triage.timer
TimersCalendar={ OnCalendar=*-*-* 06:00:00 ; next_elapse=n/a }
TimersMonotonic=
NextElapseUSecRealtime=
LastTriggerUSec=
"""
SERVICES = """Id=agent-ops-dispatcher.service
ExecMainStartTimestamp=@1795000080
ExecMainExitTimestamp=@1795000107
"""


def test_timers_lists_unloaded_and_monotonic(monkeypatch):
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        if "list-unit-files" in cmd:
            out = LIST if "--user" in cmd else ""
        elif cmd[-1].startswith("Id,Exec"):
            out = SERVICES if "--user" in cmd else ""
        else:
            out = TIMERS if "--user" in cmd else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    timers = {t["unit"]: t for t in box_status.systemd_timers()}

    assert set(timers) == {"agent-ops-dispatcher.timer", "agent-ops-triage.timer"}
    assert not any("foo@.timer" in c for c in calls)
    d = timers["agent-ops-dispatcher.timer"]
    assert describe_schedule(d["on_calendar"]) == "every 5 min" and d["interval_seconds"] == 300
    assert timers["agent-ops-triage.timer"]["interval_seconds"] is None
    assert d["next_run"] == "2026-11-18T11:13:00+00:00"
    assert d["last_run"]["duration_seconds"] == 27
    t = timers["agent-ops-triage.timer"]
    assert t["active_state"] == "inactive" and t["last_run"] is None and t["next_run"] is None
    assert d["triggers"] == {"calendar": [], "every_seconds": 300}
    assert t["triggers"] == {"calendar": ["*-*-* 06:00:00"], "every_seconds": None}


def test_monotonic_schedule_beyond_an_hour_keeps_its_words():
    assert box_status._monotonic_schedule("{ OnUnitActiveUSec=2h ; next_elapse=n/a }") == ("every 2h", 7200)


def test_next_run_prefers_the_realtime_elapse():
    t = {"NextElapseUSecRealtime": "@1795000080", "LastTriggerUSec": "@1", "ActiveState": "active"}
    assert box_status._next_run(t, 300) == "2026-11-18T11:08:00+00:00"


def test_systemctl_failure_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **_: SimpleNamespace(returncode=1, stdout="", stderr="boom\n"))
    try:
        box_status.systemd_timers()
    except RuntimeError as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_describe_schedule_words():
    assert describe_schedule("*-*-* 06,18:00:00") == "06:00, 18:00 UTC"
    assert describe_schedule("*-*-* 6:30") == "daily at 06:30 UTC"
    assert describe_schedule("Mon *-*-* 03:00") == "Mon *-*-* 03:00"
    assert describe_schedule(None) == ""


def test_upcoming_follows_the_probed_interval_not_the_words():
    from web.services import _upcoming
    unit = {"active_state": "active", "next_run": "2026-11-18T12:00:00+00:00",
            "on_calendar": "every 2h", "interval_seconds": 7200}
    assert _upcoming(unit) == ["2026-11-18T12:00:00+00:00", "2026-11-18T14:00:00+00:00",
                               "2026-11-18T16:00:00+00:00"]


ANALYZE = """  Original form: Mon..Fri *-*-* 09:00 Europe/Madrid
Normalized form: Mon..Fri *-*-* 09:00:00 Europe/Madrid
    Next elapse: Fri 2026-11-27 08:00:00 UTC
       From now: 2 days left
       Iter. #2: Mon 2026-11-30 08:00:00 UTC
       From now: 5 days left
       Iter. #3: Tue 2026-12-01 08:00:00 UTC
       From now: 6 days left
"""


def test_calendar_runs_walks_the_expression_with_systemd_analyze(monkeypatch):
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw["env"]["TZ"]))
        return SimpleNamespace(returncode=0, stdout=ANALYZE, stderr="")
    monkeypatch.setattr(box_status.subprocess, "run", run)

    assert services_config.calendar_runs("Mon..Fri *-*-* 09:00 Europe/Madrid") == [
        "2026-11-27T08:00:00+00:00", "2026-11-30T08:00:00+00:00", "2026-12-01T08:00:00+00:00"]
    assert seen == [(["systemd-analyze", "calendar", "--iterations=3", "Mon..Fri *-*-* 09:00 Europe/Madrid"], "UTC")]


def test_calendar_runs_is_empty_when_systemd_analyze_fails(monkeypatch):
    monkeypatch.setattr(box_status.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="bad"))
    assert services_config.calendar_runs("every tuesday-ish") == []


def test_calendar_normalized_is_systemds_normalized_form_with_its_timezone(monkeypatch):
    seen = []
    monkeypatch.setattr(box_status.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or SimpleNamespace(returncode=0, stdout=ANALYZE, stderr=""))
    assert services_config.calendar_normalized("Mon..Fri *-*-* 09:00 Europe/Madrid") == "Mon..Fri *-*-* 09:00:00 Europe/Madrid"
    assert seen[0][-1] == "Mon..Fri *-*-* 09:00 Europe/Madrid"


def test_calendar_normalized_is_none_when_systemd_analyze_fails(monkeypatch):
    monkeypatch.setattr(box_status.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="bad"))
    assert services_config.calendar_normalized("every tuesday-ish") is None
