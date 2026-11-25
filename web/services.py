"""`GET /api/services`: the box's recurring jobs, from the systemd timers the probe reports."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dispatcher import services_changes
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


class ServicesView(BaseModel):
    now: str
    managed: list[Job]
    read_only: list[Job]


def _daily(hours: str, minute: str) -> str:
    times = [f"{int(h):02d}:{minute}" for h in hours.split(",")]
    return f"daily at {times[0]} UTC" if len(times) == 1 else f"{', '.join(times)} UTC"


def describe_schedule(calendar: str | None) -> str:
    """A systemd OnCalendar in words; an expression not recognised is shown as is."""
    calendar = calendar or ""
    if m := re.fullmatch(r"\*:0/(\d+)", calendar):
        return f"every {m[1]} min"
    if m := re.fullmatch(r"\*-\*-\* ([\d,]+):(\d\d)(?::00)?", calendar):
        return _daily(m[1], m[2])
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
    first = datetime.fromisoformat(unit["next_run"])
    step = unit.get("interval_seconds") or _calendar_interval(unit["on_calendar"])
    count = UPCOMING if step else 1  # ponytail: multi-time schedules list one run, add a calendar walk if needed
    return [(first + timedelta(seconds=step * i)).isoformat() for i in range(count)]


def _recent(runs: list[dict]) -> list[RecentRun]:
    return [RecentRun(at=r["at"], duration_seconds=r["duration_seconds"],
                      result=r["result"] or "running") for r in reversed(runs[-RECENT:])]


def _change(entry: dict | None) -> Change | None:
    if not entry:
        return None
    rec = entry.get("record") or {}
    return Change(state="failed" if rec.get("failed") else "applying",
                  pr=rec.get("pr"), url=rec.get("url"))


def _job(unit: dict, runs: dict[str, list[dict]], *, read_only: bool,
         changes: dict | None = None) -> Job:
    schedule = describe_schedule(unit["on_calendar"])
    job_id = unit["unit"].removesuffix(".timer")
    mine = runs.get(job_id, [])
    running = not read_only and bool(mine) and mine[-1]["result"] is None
    return Job(
        recent_runs=[] if read_only else _recent(runs.get(job_id, [])),
        upcoming=_upcoming(unit),
        change=None if read_only else _change((changes or {}).get(job_id.removeprefix(USER_JOB_PREFIX))),
        id=job_id, name=unit["description"],
        description=unit["description"],
        state="running" if running else "active" if unit["active_state"] == "active" else "paused",
        schedule=schedule,
        last_run=unit["last_run"], next_run=unit["next_run"], read_only=read_only)


def _managed(t: dict) -> bool:
    return t["scope"] == "user" and t["unit"].startswith(USER_JOB_PREFIX) and t["unit"].endswith(".timer")


def _read_only(t: dict) -> bool:
    return (t["scope"] == "system" and t["fragment_path"].startswith(ROOT_UNIT_DIR)
            and t["unit"].endswith(".timer"))


def services_view(timers: list[dict], runs: dict[str, list[dict]], now: datetime,
                  changes: dict | None = None) -> ServicesView:
    """Managed: agent-ops user timers. Read-only: root timers whose unit file is
    in /etc/systemd/system (Ubuntu's own timers live elsewhere)."""
    return ServicesView(
        now=now.isoformat(),
        managed=[_job(t, runs, read_only=False, changes=changes) for t in timers if _managed(t)],
        read_only=[_job(t, runs, read_only=True) for t in timers if _read_only(t)])


def router(sources, now: Callable[[], datetime], state_dir: Callable[[], str]) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/services", response_model=ServicesView)
    def services(op: Operator = Depends(current_operator)):
        return services_view(sources.service_timers(), sources.service_runs(), now().astimezone(timezone.utc),
                             services_changes.read(state_dir()))

    def check_managed(job: str) -> None:
        if not any(j.id == job for j in services_view(
                sources.service_timers(), {}, now()).managed):
            raise HTTPException(404, "not a managed job")

    def switch(job: str, paused: bool) -> dict:
        check_managed(job)
        change = sources.disable_timer if paused else sources.enable_timer
        try:
            services_changes.set_paused(state_dir(), job.removeprefix(USER_JOB_PREFIX), paused,
                                        lambda: change(f"{job}.timer"))
        except Exception as e:
            raise HTTPException(502, f"systemctl failed: {e}") from e
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
