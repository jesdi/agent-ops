"""`GET /api/services`: the box's recurring jobs, from the systemd timers the probe reports."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from dispatcher import services_changes, services_config
from web.auth import Operator, current_operator

USER_JOB_PREFIX = "agent-ops-"
ROOT_UNIT_DIR = "/etc/systemd/system/"


class Run(BaseModel):
    at: str
    duration_seconds: int | None = None


class RecentRun(BaseModel):
    at: str
    duration_seconds: int | None = None
    result: str         # "success" | "failed" | "stopped" | "running"


class Change(BaseModel):
    """A console change not yet installed: "applying", or "failed" to record."""
    state: str
    pr: int | None = None
    url: str | None = None


class Job(BaseModel):
    id: str
    name: str
    description: str
    state: str          # "active" | "paused" | "running"
    schedule: str
    last_run: Run | None
    next_run: str | None
    read_only: bool
    recent_runs: list[RecentRun] = []
    upcoming: list[str] = []
    change: Change | None = None
    warning: str | None = None


class ServicesView(BaseModel):
    now: str
    config_installed: bool
    managed: list[Job]
    read_only: list[Job]


def _daily(hours: str, minute: str, zone: str | None) -> str:
    times = [f"{int(h):02d}:{minute}" for h in hours.split(",")]
    zone = zone or "UTC"
    return f"daily at {times[0]} {zone}" if len(times) == 1 else f"{', '.join(times)} {zone}"


def describe_schedule(calendar: str | None) -> str:
    """A systemd OnCalendar in words; an expression not recognised is shown as is."""
    calendar = calendar or ""
    if m := re.fullmatch(r"\*:0/(\d+)", calendar):
        return f"every {m[1]} min"
    if m := re.fullmatch(r"\*-\*-\* ([\d,]+):(\d\d)(?::00)?(?: ([A-Za-z0-9_+\-/]+))?", calendar):
        return _daily(m[1], m[2], m[3])
    return calendar


RECENT, UPCOMING = 5, 3


def _calendar_interval(calendar: str | None) -> int:
    """Seconds between runs of a systemd OnCalendar that repeats evenly; 0 otherwise."""
    if m := re.fullmatch(r"\*:0/(\d+)", calendar or ""):
        return int(m[1]) * 60
    return 86400 if re.fullmatch(r"\*-\*-\* \d{1,2}:\d\d(?::00)?", calendar or "") else 0


def _upcoming(unit: dict) -> list[str]:
    if unit["active_state"] != "active" or not unit["next_run"]:
        return []
    if unit.get("upcoming"):  # the probe's calendar walk, by systemd itself
        return unit["upcoming"][:UPCOMING]
    return _stepped(datetime.fromisoformat(unit["next_run"]),
                    unit.get("interval_seconds") or _calendar_interval(unit["on_calendar"]))


def _stepped(first: datetime, step: int) -> list[str]:
    """Runs `step` seconds apart from `first`; only `first` when no step is known."""
    count = UPCOMING if step else 1
    return [(first + timedelta(seconds=step * i)).isoformat() for i in range(count)]


def _recent(runs: list[dict]) -> list[RecentRun]:
    return [RecentRun(at=r["at"], duration_seconds=r["duration_seconds"],
                      result=r["result"] or "running") for r in reversed(runs[-RECENT:])]


def _change(entry: dict | None) -> Change | None:
    """A failed recording of any change, or a schedule change applying. A pause or resume
    still being recorded is none: it holds on the box already (requirements 12, 17)."""
    rec = (entry or {}).get("record") or {}
    if not (rec.get("failed") or "schedule" in (entry or {})):
        return None
    link = rec["failed"] if isinstance(rec.get("failed"), dict) else rec
    return Change(state="failed" if rec.get("failed") else "applying",
                  pr=link.get("pr"), url=link.get("url"))


DIFFERS = "The box differs from the services config."


def _warning(unit: dict, key: str, changes: dict, installed: dict | None) -> str | None:
    """The box differs from the installed config with no console change behind it: its
    paused state, or the schedule its loaded timer fires on."""
    if installed is None:
        return None
    return DIFFERS if _differs(unit, key, changes.get(key) or {}, installed.get(key) or {}) else None


def _differs(unit: dict, key: str, change: dict, want: dict) -> bool:
    if "paused" not in change and _paused_differs(unit, want):
        return True
    return "schedule" not in change and _timer_edited(unit, want)


def _paused_differs(unit: dict, want: dict) -> bool:
    return (unit["active_state"] != "active") != bool(want.get("paused", False))


def _timer_edited(unit: dict, want: dict) -> bool:
    """Whether the timer systemd has loaded (unit file, drop-ins and all, as of the last
    daemon-reload) fires on another schedule than the config's; no triggers tell nothing."""
    loaded, sched = unit.get("triggers"), want.get("schedule")
    if not (loaded and sched):
        return False
    if "every" in sched:
        return bool(loaded["calendar"]) or loaded["every_seconds"] != sched["every"] * 60
    return loaded["every_seconds"] is not None or not _same_calendar(
        loaded["calendar"], services_config.calendar(sched))


def _same_calendar(loaded: list[str], want: str) -> bool:
    """One OnCalendar firing when `want` does: a different spelling is judged by the
    normalized form systemd gives both, not by a sample of runs two schedules can share."""
    if len(loaded) != 1:
        return False
    if loaded[0] == want:
        return True
    norm = services_config.calendar_normalized(loaded[0])
    return norm is not None and norm == services_config.calendar_normalized(want)


def _job(unit: dict, runs: dict[str, list[dict]], *, read_only: bool,
         changes: dict | None = None, installed: dict | None = None) -> Job:
    job_id = unit["unit"].removesuffix(".timer")
    job = Job(
        recent_runs=[], upcoming=_upcoming(unit), change=None, warning=None,
        id=job_id, name=unit["description"],
        description=unit["description"],
        state="active" if unit["active_state"] == "active" else "paused",
        schedule=describe_schedule(unit["on_calendar"]),
        last_run=unit["last_run"], next_run=unit["next_run"], read_only=read_only)
    return job if read_only else _live(job, unit, runs.get(job_id, []), changes or {}, installed)


def _live(job: Job, unit: dict, mine: list[dict], changes: dict, installed: dict | None) -> Job:
    """A managed job's runs, console change and warning."""
    key = job.id.removeprefix(USER_JOB_PREFIX)
    running = bool(mine) and mine[-1]["result"] is None
    return job.model_copy(update={
        "recent_runs": _recent(mine), "change": _change(changes.get(key)),
        "warning": _warning(unit, key, changes, installed),
        "state": "running" if running else job.state})


def _managed(t: dict) -> bool:
    return t["scope"] == "user" and t["unit"].startswith(USER_JOB_PREFIX) and t["unit"].endswith(".timer")


def _read_only(t: dict) -> bool:
    return (t["scope"] == "system" and t["fragment_path"].startswith(ROOT_UNIT_DIR)
            and t["unit"].endswith(".timer"))


def _rank(j: Job) -> int:
    if j.state == "running" or j.change or j.warning:
        return 0
    return 1 if j.state == "paused" else 2


def services_view(timers: list[dict], runs: dict[str, list[dict]], now: datetime,
                  changes: dict | None = None, installed: dict | None = None) -> ServicesView:
    """Managed: agent-ops user timers. Read-only: root timers whose unit file is
    in /etc/systemd/system (Ubuntu's own timers live elsewhere)."""
    managed = [_job(t, runs, read_only=False, changes=changes, installed=installed) for t in timers if _managed(t)]
    managed.sort(key=_rank)  # stable: the box's order within a rank
    return ServicesView(
        now=now.isoformat(),
        managed=managed,
        read_only=[_job(t, runs, read_only=True) for t in timers if _read_only(t)],
        config_installed=installed is not None)


def _deploy_sync_paused(timers: list[dict], changes: dict) -> bool:
    return _sync_off(timers) or bool((changes.get("update") or {}).get("paused"))


def _sync_off(timers: list[dict]) -> bool:
    return any(t["unit"] == f"{USER_JOB_PREFIX}update.timer" and _managed(t) and t["active_state"] != "active"
               for t in timers)


def _installed(state_dir: str) -> dict | None:
    """The installed services config, or None before the infra change has deployed."""
    f = Path(state_dir) / "services.yaml"
    return (yaml.safe_load(f.read_text()) or {}) if f.exists() else None


def router(sources, now: Callable[[], datetime], state_dir: Callable[[], str]) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/services", response_model=ServicesView)
    def services(op: Operator = Depends(current_operator)):
        return services_view(sources.service_timers(), sources.service_runs(), now().astimezone(timezone.utc),
                             services_changes.read(state_dir()), _installed(state_dir()))

    def check_managed(job: str) -> None:
        if not any(j.id == job for j in services_view(
                sources.service_timers(), {}, now()).managed):
            raise HTTPException(404, "not a managed job")

    def check_installed() -> None:
        """Pause, Resume and a schedule are recorded in the services config: refused before it
        is installed, since nothing would apply or record them."""
        if _installed(state_dir()) is None:
            raise HTTPException(409, "The services config is not installed on the box yet.")

    def switch(job: str, paused: bool) -> dict:
        check_managed(job)
        check_installed()
        change = sources.disable_timer if paused else sources.enable_timer
        try:
            services_changes.set_paused(state_dir(), job.removeprefix(USER_JOB_PREFIX), paused,
                                        lambda: change(f"{job}.timer"))
        except Exception as e:
            raise HTTPException(502, f"systemctl failed: {e}") from e
        sources.kick_recorder()
        return {}

    @routes.put("/api/services/{job}/schedule")
    def set_schedule(job: str, schedule: dict = Body(...), op: Operator = Depends(current_operator)):
        check_managed(job)
        name = job.removeprefix(USER_JOB_PREFIX)
        if name not in services_config.JOBS:
            raise HTTPException(404, "not a managed job")
        check_installed()
        try:
            services_config.check_schedule(name, schedule)
        except ValueError as e:
            raise HTTPException(422, str(e).removeprefix(f"{name}: ")) from e
        if _deploy_sync_paused(sources.service_timers(), services_changes.read(state_dir())):
            raise HTTPException(409, "The deploy sync is paused: a schedule change cannot be applied.")
        try:
            services_changes.set_schedule(state_dir(), name, schedule)
        except services_changes.Applying:
            raise HTTPException(409, "A schedule change is already applying.") from None
        sources.kick_recorder()
        return {}

    @routes.post("/api/services/{job}/retry")
    def retry(job: str, op: Operator = Depends(current_operator)):
        check_managed(job)
        services_changes.update(state_dir(), job.removeprefix(USER_JOB_PREFIX), lambda e: {
            **e, "record": {k: v for k, v in (e.get("record") or {}).items() if k != "failed"}
        } if e else e)
        sources.kick_recorder()
        return {}

    @routes.post("/api/services/{job}/pause")
    def pause(job: str, op: Operator = Depends(current_operator)):
        return switch(job, True)

    @routes.post("/api/services/{job}/resume")
    def resume(job: str, op: Operator = Depends(current_operator)):
        return switch(job, False)

    def act(job: str, verb: str) -> dict:
        check_managed(job)
        (sources.start_unit if verb == "start" else sources.stop_unit)(f"{job}.service")
        return {}

    @routes.post("/api/services/{job}/run")
    def run_now(job: str, op: Operator = Depends(current_operator)):
        return act(job, "start")

    @routes.post("/api/services/{job}/stop")
    def stop_run(job: str, op: Operator = Depends(current_operator)):
        return act(job, "stop")

    return routes
