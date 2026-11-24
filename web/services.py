"""`GET /api/services`: the box's recurring jobs, from the systemd timers the probe reports."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from web.auth import Operator, current_operator

USER_JOB_PREFIX = "agent-ops-"
ROOT_UNIT_DIR = "/etc/systemd/system/"


class Run(BaseModel):
    at: str
    duration_seconds: int | None = None


class Job(BaseModel):
    id: str
    name: str
    description: str
    state: str          # "active" | "paused"
    schedule: str
    last_run: Run | None
    next_run: str | None
    read_only: bool


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


def _job(unit: dict, *, read_only: bool) -> Job:
    return Job(
        id=unit["unit"].removesuffix(".timer"), name=unit["description"],
        description=unit["description"],
        state="active" if unit["active_state"] == "active" else "paused",
        schedule=describe_schedule(unit["on_calendar"]),
        last_run=unit["last_run"], next_run=unit["next_run"], read_only=read_only)


def _managed(t: dict) -> bool:
    return t["scope"] == "user" and t["unit"].startswith(USER_JOB_PREFIX) and t["unit"].endswith(".timer")


def _read_only(t: dict) -> bool:
    return (t["scope"] == "system" and t["fragment_path"].startswith(ROOT_UNIT_DIR)
            and t["unit"].endswith(".timer"))


def services_view(timers: list[dict], now: datetime) -> ServicesView:
    """Managed: agent-ops user timers. Read-only: root timers whose unit file is
    in /etc/systemd/system (Ubuntu's own timers live elsewhere)."""
    return ServicesView(
        now=now.isoformat(),
        managed=[_job(t, read_only=False) for t in timers if _managed(t)],
        read_only=[_job(t, read_only=True) for t in timers if _read_only(t)])


def router(sources, now: Callable[[], datetime]) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/services", response_model=ServicesView)
    def services(op: Operator = Depends(current_operator)):
        return services_view(sources.service_timers(), now().astimezone(timezone.utc))

    return routes
