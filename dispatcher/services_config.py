"""Services config: validate it, and render it into the timers of the managed jobs.

    python -m dispatcher.services_config validate <file>
    python -m dispatcher.services_config render <config> <changes> <unit-dir>

`render` prints JSON `{"enabled": [...], "disabled": [...]}` of timer file names.
Only a change's `paused` overrides the config; its `schedule` is never applied.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

JOBS = ("dispatcher", "update", "onboard", "keepalive", "codex-keepalive", "sweep", "digest", "triage")
_DAILY = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_TZ = re.compile(r"[A-Za-z0-9_+\-/]+")
_KINDS = {"every", "daily", "calendar"}


def _check_every(job: str, value, sched: dict) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{job}: every must be a whole number of at least 1")


def _check_daily(job: str, value, sched: dict) -> None:
    if not isinstance(value, str) or not _DAILY.fullmatch(value):
        raise ValueError(f"{job}: daily must be HH:MM")
    _check_tz(job, sched.get("tz"))


def _check_tz(job: str, tz) -> None:
    if not isinstance(tz, str) or not _TZ.fullmatch(tz) or tz.startswith("/"):
        raise ValueError(f"{job}: daily needs a timezone")
    try:
        ZoneInfo(tz)
    except Exception:
        raise ValueError(f"{job}: unknown timezone {tz!r}") from None


def _check_calendar(job: str, value, sched: dict) -> None:
    if not isinstance(value, str) or not value.isprintable() or not value.strip() or value.startswith("-"):
        raise ValueError(f"{job}: calendar must be one printable line")
    r = subprocess.run(["systemd-analyze", "calendar", value], capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"{job}: calendar {value!r} is not accepted by systemd")


_CHECKS = {"every": _check_every, "daily": _check_daily, "calendar": _check_calendar}


def _check_schedule(job: str, sched) -> None:
    if not isinstance(sched, dict):
        raise ValueError(f"{job}: schedule must be a map")
    kinds = _KINDS & sched.keys()
    if len(kinds) != 1:
        raise ValueError(f"{job}: schedule needs exactly one of every, daily, calendar")
    kind = kinds.pop()
    extra = sched.keys() - {kind} - ({"tz"} if kind == "daily" else set())
    if extra:
        raise ValueError(f"{job}: unknown schedule keys {sorted(map(str, extra))}")
    _CHECKS[kind](job, sched[kind], sched)


def _job_map(raw, what: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be a map of jobs")
    for job in raw:
        if job not in JOBS:
            raise ValueError(f"{job}: not a managed job")
    return raw


def _check_entry(job: str, entry) -> None:
    if not isinstance(entry, dict):
        raise ValueError(f"{job}: missing")
    if not isinstance(entry.get("paused"), bool):
        raise ValueError(f"{job}: paused must be true or false")
    _check_schedule(job, entry.get("schedule"))


def load(path: str) -> dict:
    """The validated config at `path`; ValueError naming the job otherwise."""
    cfg = _job_map(yaml.safe_load(Path(path).read_text()), "config")
    for job in JOBS:
        _check_entry(job, cfg.get(job))
    return cfg


def _when(sched: dict) -> str:
    if "every" in sched:
        return f"OnBootSec=1min\nOnUnitActiveSec={sched['every']}min"
    if "daily" in sched:
        return f"OnCalendar=*-*-* {sched['daily']}:00 {sched['tz']}"
    return f"OnCalendar={sched['calendar']}"


def _timer(job: str, sched: dict) -> str:
    return (f"[Unit]\nDescription=agent-ops {job}\n\n[Timer]\n{_when(sched)}\n"
            f"Unit=agent-ops-{job}.service\n\n[Install]\nWantedBy=timers.target\n")


def render(config: str, changes: str, unit_dir: str) -> dict:
    cfg = load(config)
    ch = _job_map(yaml.safe_load(Path(changes).read_text()) or {}, "changes")
    out: dict[str, list[str]] = {"enabled": [], "disabled": []}
    for job in JOBS:
        name = f"agent-ops-{job}.timer"
        (Path(unit_dir) / name).write_text(_timer(job, cfg[job]["schedule"]))
        paused = (ch.get(job) or {}).get("paused", cfg[job]["paused"])
        out["disabled" if paused else "enabled"].append(name)
    return out


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "validate":
            load(argv[1])
        elif len(argv) == 4 and argv[0] == "render":
            print(json.dumps(render(*argv[1:]), indent=2))
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (ValueError, OSError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
