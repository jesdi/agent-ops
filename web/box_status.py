"""`agent-ops status`: a read-only report of the box from local state alone.

Nothing here writes under the state dir, takes a lock or calls the network:
usage comes from the dispatcher's on-disk cache and the outside sources are
the injected `Probes`, which only query."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, NamedTuple, TextIO
from urllib.parse import quote

from dispatcher import config, herdr, services_config, sessions, state, triage
from dispatcher.status import HumanReason, Kind, ended, occupies
from dispatcher.usage import unavailable
from dispatcher.usage_providers import cache_path, usage_from_json
from web import read_model

TIMER = "agent-ops-dispatcher.timer"
SERVICE = "agent-ops-dispatcher.service"


class Unit(NamedTuple):
    state: str          # systemd ActiveState: active, inactive, failed, ...
    since: datetime     # when it entered that state


@dataclass(frozen=True)
class Probes:
    """Every outside source the report reads; each one only queries.
    units: every `agent-ops-*` user unit and timer, by name.
    tabs: the live herdr tab labels.
    journal: the `agent-ops` journal entries (.time, .unit, .priority, .message),
    oldest first; raises with journalctl's stderr when it fails."""
    units: Callable[[], dict[str, Unit]]
    tabs: Callable[[], set[str]]
    journal: Callable[[], list] = lambda: []
    timers: Callable[[], list[dict]] = lambda: []


def system_probes() -> Probes:
    return Probes(units=_systemd_units, tabs=_herdr_tabs, journal=_journal,
                  timers=systemd_timers)


def _systemctl(scope: str) -> list[str]:
    return ["systemctl", "--user"] if scope == "user" else ["systemctl"]


def _systemctl_out(scope: str, *args: str) -> str:
    proc = subprocess.run([*_systemctl(scope), *args], capture_output=True, text=True, timeout=10)
    if proc.returncode:
        raise RuntimeError(f"systemctl failed: {proc.stderr.strip()}")
    return proc.stdout


def _installed_timers(scope: str) -> list[str]:
    """Installed timer names, loaded or not: `show '*.timer'` misses an
    inactive (e.g. disabled after reboot) timer, which must still list as paused."""
    out = _systemctl_out(scope, "list-unit-files", "*.timer", "--no-legend", "--no-pager")
    names = [l.split()[0] for l in out.splitlines() if l.strip()]
    return [n for n in names if "@." not in n]  # a template is no job; show fails on it


def _props(block: str) -> dict[str, str]:
    return dict(l.split("=", 1) for l in block.splitlines() if "=" in l)


def _show(scope: str, names: list[str], props: str) -> list[dict[str, str]]:
    if not names:
        return []
    out = _systemctl_out(scope, "show", "--timestamp=unix", *names, "-p", props)
    return [_props(block) for block in out.split("\n\n") if block.strip()]


def _iso(stamp: str | None) -> str | None:
    stamp = (stamp or "").lstrip("@")
    return (datetime.fromtimestamp(int(stamp), timezone.utc).isoformat()
            if stamp.isdigit() else None)


_DURATION_UNITS = {"d": 86400, "h": 3600, "min": 60, "m": 60, "s": 1}


def _seconds(words: str) -> int:
    return sum(int(n) * _DURATION_UNITS.get(u, 0) for n, u in re.findall(r"(\d+)\s*([a-z]+)", words))


def _monotonic_schedule(timers_monotonic: str) -> tuple[str | None, int | None]:
    """(schedule, interval seconds) of an `OnUnitActiveUSec=5min` timer; the
    schedule is an OnCalendar-style `*:0/N` for whole minutes, else raw words."""
    m = re.search(r"OnUnitActiveUSec=([^;}]*?)\s*(?:;|\})", timers_monotonic)
    if not m:
        return None, None
    seconds = _seconds(m[1])
    if seconds and seconds % 60 == 0 and seconds < 3600:
        return f"*:0/{seconds // 60}", seconds
    return f"every {m[1]}", seconds or None


def _last_run(svc: dict[str, str]) -> dict | None:
    start, end = _iso(svc.get("ExecMainStartTimestamp")), _iso(svc.get("ExecMainExitTimestamp"))
    if not (start and end and end >= start):
        return None
    return {"at": start, "duration_seconds": int(
        (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())}


def _next_run(t: dict[str, str], interval: int | None) -> str | None:
    """A monotonic timer has no realtime elapse: next = last trigger + interval."""
    if realtime := _iso(t.get("NextElapseUSecRealtime")):
        return realtime
    trigger = _iso(t.get("LastTriggerUSec"))
    if trigger and interval and t["ActiveState"] == "active":
        return (datetime.fromisoformat(trigger) + timedelta(seconds=interval)).isoformat()
    return None


def _timer(scope: str, t: dict[str, str], services: dict[str, dict]) -> dict:
    calendar = re.search(r"OnCalendar=([^;}]*?)\s*(?:;|\})", t.get("TimersCalendar", ""))
    monotonic, interval = _monotonic_schedule(t.get("TimersMonotonic", ""))
    return {
        "scope": scope, "unit": t["Id"], "fragment_path": t.get("FragmentPath", ""),
        "description": t.get("Description", ""), "active_state": t["ActiveState"],
        "on_calendar": calendar[1] if calendar else monotonic,
        "interval_seconds": None if calendar else interval,
        "last_run": _last_run(services.get(t["Id"].removesuffix(".timer") + ".service", {})),
        "next_run": _next_run(t, interval)}


def systemd_timers() -> list[dict]:
    """Every installed timer, user and system, with its service's last run.
    Services are told apart by the caller; a timer here is a `.timer` unit."""
    out = []
    for scope in ("user", "system"):
        names = _installed_timers(scope)
        services = {p["Id"]: p for p in _show(
            scope, [n.removesuffix(".timer") + ".service" for n in names],
            "Id,ExecMainStartTimestamp,ExecMainExitTimestamp")}
        out += [_timer(scope, t, services) for t in _show(
            scope, names, "Id,Description,ActiveState,FragmentPath,TimersCalendar,"
            "TimersMonotonic,NextElapseUSecRealtime,LastTriggerUSec")]
    return out


def _systemd_units() -> dict[str, Unit]:
    proc = subprocess.run(
        ["systemctl", "--user", "show", "--timestamp=unix", "agent-ops-*",
         "-p", "Id,ActiveState,StateChangeTimestamp"],
        capture_output=True, text=True, timeout=10)
    if proc.returncode:
        raise RuntimeError(f"systemctl failed: {proc.stderr.strip()}")
    return parse_units(proc.stdout)


def parse_units(show: str) -> dict[str, Unit]:
    """`systemctl show --timestamp=unix` output: one blank-line-separated
    block per unit; a unit that never changed state has no stamp and is skipped."""
    units = {}
    for block in show.split("\n\n"):
        props = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
        stamp = props.get("StateChangeTimestamp", "").lstrip("@")
        if props.get("Id") and stamp.isdigit():
            units[props["Id"]] = Unit(props["ActiveState"],
                                      datetime.fromtimestamp(int(stamp)).astimezone())
    return units


def _journal(since: str | None = "6 hours ago", unit: str = "agent-ops-*", lines: int | None = None) -> list:
    window = ["--since", since] if since else ["-n", str(lines)]
    proc = subprocess.run(
        ["journalctl", "--user", "-u", unit, *window, "-o", "json", "--no-pager"],
        capture_output=True, text=True, timeout=20)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
    return parse_journal(proc.stdout)


def parse_journal(output: str) -> list:
    """`journalctl -o json` lines; a malformed line is skipped."""
    entries = []
    for line in output.splitlines():
        try:
            j = json.loads(line)
            msg = j.get("MESSAGE")
            entries.append(SimpleNamespace(
                time=datetime.fromtimestamp(int(j["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc),
                unit=j.get("_SYSTEMD_USER_UNIT") or j.get("USER_UNIT") or "",
                priority=int(j.get("PRIORITY", 6)),
                message=msg if isinstance(msg, str) else ""))
        except (ValueError, KeyError, TypeError):
            continue
    return entries


_RUN_ENDS = (("Deactivated successfully", "success"), ("Finished ", "success"),
             ("Failed with result", "failed"),
             ("Stopped ", "stopped"))


def _pending(mine: list[dict]) -> dict | None:
    """The last run when it has no end line yet."""
    return mine[-1] if mine and mine[-1]["result"] is None else None


def _open_run(mine: list[dict], e) -> None:
    if _pending(mine) and e.message.startswith("Started "):
        return  # Type=notify logs Starting then Started: one run
    mine.append({"at": e.time.isoformat(), "duration_seconds": None, "result": None})


def _close_run(mine: list[dict], e, stopping: bool) -> None:
    result = next((r for word, r in _RUN_ENDS if word in e.message), None)
    run = _pending(mine)
    if result and run:
        started = datetime.fromisoformat(run["at"])
        run.update(result="stopped" if stopping else result,
                   duration_seconds=int((e.time - started).total_seconds()))


def journal_runs(entries: list) -> dict[str, list[dict]]:
    """Runs per job id (service name without `.service`), oldest first, from
    systemd's own lines: `Starting` (oneshot) or `Started` opens a run, the next end line closes it
    (a run with no end line yet is still going: no duration, no result).
    An operator stop logs `Stopping` first: the run stays open until its end
    line, then ends as stopped whatever that line says."""
    runs: dict[str, list[dict]] = {}
    stopping: set[str] = set()
    for e in entries:
        if not e.unit.endswith(".service"):
            continue
        job = e.unit.removesuffix(".service")
        mine = runs.setdefault(job, [])
        if e.message.startswith(("Starting ", "Started ")):
            _open_run(mine, e)
            stopping.discard(job)
        elif e.message.startswith("Stopping "):
            stopping.add(job)
        else:
            _close_run(mine, e, job in stopping)
            if not _pending(mine):
                stopping.discard(job)
    return runs


RECENT_RUNS = 5


def service_runs(journal: Callable[..., list] = _journal) -> dict[str, list[dict]]:
    """Journal probe for the Services page: a week of runs, then, for a managed job with
    fewer than five in it (a weekly or long-paused job), the latest lines of its own journal."""
    runs = journal_runs(journal("7 days ago"))
    for name in services_config.JOBS:
        job = f"agent-ops-{name}"
        if len(runs.get(job, [])) < RECENT_RUNS:
            # ponytail: a run logging more than this many lines leaves fewer than five; filter
            # journalctl to the manager's own lines if a sparse job turns chatty.
            older = journal_runs(journal(None, f"{job}.service", 2000)).get(job)
            if older:
                runs[job] = older
    return runs


def _herdr_tabs() -> set[str]:
    tabs = herdr.tab_labels()
    if tabs is None:
        raise RuntimeError("herdr socket missing")
    return tabs


def _hhmm(ts: datetime, now: datetime) -> str:
    return ts.astimezone(now.tzinfo).strftime("%H:%M")


def _minutes(since: datetime, now: datetime) -> int:
    return int((now - since).total_seconds() // 60)


def _json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _time(raw: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return t if t.tzinfo else None  # a naive stamp can't be compared with the aware clock


def _usages(cfg: config.Config, root: Path, now: datetime) -> dict:
    """Each routed provider's cached reading, whatever its age (the usage
    cache is only ever read here, never refreshed)."""
    out = {}
    for name in sorted(config.referenced_providers(cfg)):
        try:
            out[name] = usage_from_json(
                json.loads(cache_path(root, name).read_text())["usage"])
        except (OSError, KeyError, TypeError, ValueError):
            out[name] = unavailable(name, now.timestamp())
    return out


def _quarantine(root: Path) -> list[str]:
    lines = []
    for p in sorted((root / "quarantine").glob("*.json")):
        parsed, rec = state.parse_task_key(p.stem), _json(p)
        if parsed and rec is None:
            lines.append(f"unknown (unreadable quarantine/{p.name})")
        elif parsed:
            lines.append(f"quarantine: {parsed[0]}#{parsed[1]} blocked by "
                         f"{rec.get('blocker_repo')}#{rec.get('blocker_issue')}")
    return lines


def _verdict(cfg, root, now, units, tabs, tasks, hb, usages, marker) -> str:
    projects = {r.name: r.project for r in cfg.repositories}
    triage_tab = tabs is not None and triage.TAB_LABEL in tabs
    request = _time((_json(root / triage.REQUEST_FILE) or {}).get("requested_at"))
    gate = read_model.usage_view(
        usages, now=now, pace=cfg.pace, default_model=cfg.models.gate_entry().model_id,
        mode="auto", routed=usages.keys(), pinned=cfg.models.pinned).gate
    blocker = read_model.local_blocker(
        hb, now=now, tasks=tasks, capacity=cfg.capacity, gate=gate,
        claims_paused=triage_tab or (root / triage.REQUEST_FILE).exists(),
        triage_running=triage_tab, projects=projects, max_open=cfg.max_open)
    if blocker == "dispatcher-down":
        return _stale_heartbeat(root, units, hb, now)
    timer_down = _timer_down(units)
    if timer_down:
        return timer_down
    if blocker is None:
        return "no local blocker — candidates need GitHub (see console)"
    return f"not claiming: {blocker} ({_evidence(blocker, gate, usages, marker, request, projects, tasks, cfg, now, triage_tab)})"


def _stale_heartbeat(root, units, hb, now) -> str:
    """A stale heartbeat while the service runs is a long pass, not a dead
    dispatcher. A oneshot service sits in `activating` for its whole run."""
    service = (units or {}).get(SERVICE)
    if service and service.state in ("active", "activating"):
        return f"dispatcher busy (pass running since {_hhmm(service.since, now)})"
    return f"not claiming: dispatcher-down ({_last_pass(root, hb, now)})"


def _timer_down(units) -> str | None:
    """Unknown units (a failed probe) say nothing about the timer."""
    if units is None:
        return None
    timer = units.get(TIMER)
    if timer is not None and timer.state == "active":
        return None
    return f"not claiming: dispatcher-down ({TIMER} {timer.state if timer else 'not found'})"


def _last_pass(root: Path, hb: dict | None, now: datetime) -> str:
    """The heartbeat in words; a file that is there but unusable is not "no pass.json"."""
    if hb is None:
        return ("unknown (unreadable pass.json)" if (root / "pass.json").exists()
                else "no pass.json")
    finished = _time(hb.get("finished_at"))
    if finished is None:
        return "unknown (pass.json finished_at missing or naive)"
    return (f"last pass {_minutes(finished, now)}m ago, "
            + ("custom schedule" if hb.get("custom") else f"interval {hb.get('interval_minutes')}m"))


def _evidence(name, gate, usages, marker, request, projects, tasks, cfg, now, triage_tab):
    if name == "claims-paused":
        return (f"{triage.REQUEST_FILE}, {_minutes(request, now)}m old" if request
                else "triage tab live")
    if name == "auth-dark":
        since = _time((marker or {}).get("since"))
        return (f"usage unknowable since {_hhmm(since, now)} — re-login likely"
                if since else "usage unknowable")
    if name == "budget-blocked":
        age = round((now.timestamp() - usages[gate.provider].fetched_at) / 60)
        return f"resets in {round(gate.minutes_to_reset)}m, usage read {age}m ago"
    if name == "projects-paused":
        return ", ".join(sorted({p.name for p in projects.values()})) + " paused in targets.yaml"
    return f"{_used(tasks, triage_tab)}/{cfg.capacity} slots"


def _used(tasks, triage_tab: bool) -> int:
    return sum(occupies(t.status) for t in tasks) + int(triage_tab)


def _dispatcher_row(root, hb, units, now) -> str:
    last = _last_pass(root, hb, now)
    if units is None:
        return f"{last}, timer unknown"
    timer = units.get(TIMER)
    return f"{last}, timer {timer.state if timer else 'not found'}"


def _units_row(units, now) -> str:
    failed = [f"{n} failed {_hhmm(u.since, now)}" for n, u in sorted(units.items())
              if u.state == "failed"]
    return ", ".join(failed) or "all active"


def _holds(root, request, marker, now) -> str:
    holds = []
    if request:
        holds.append(f"triage request ({_minutes(request, now)}m old)")
    if (root / "budget-stalled").exists():
        holds.append("budget-stalled")
    since = _time((marker or {}).get("since"))
    if marker is not None:
        holds.append(f"auth-dark since {_hhmm(since, now)}" if since else "auth-dark")
    return " · ".join(holds + _quarantine(root)) or "none"


def _age(task: state.TaskState, now: datetime) -> str:
    since = _time(task.updated_at)
    if since is None:
        return "?"
    m = _minutes(since, now)
    return f"{m}m" if m < 60 else f"{m // 60}h" if m < 2880 else f"{m // 1440}d"


def _unreadable(root: Path) -> list[str]:
    """Task files `load_all` would skip (corrupt), named by their file key. Each
    file is re-read on its own, so a valid twin cannot hide a corrupt one."""
    out = []
    for p in sorted(root.glob("task-*.json")):
        try:
            state._read(p)
        except FileNotFoundError:
            continue
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            key = p.stem[len("task-"):]
            parsed = state.parse_task_key(key)
            out.append(f"{parsed[0]}#{parsed[1]}" if parsed else p.stem)
    return out


def _task_line(t: state.TaskState, now, tabs, tabs_why) -> str:
    ref = f"{t.repository}#{t.issue}"
    word = _park_word(t)
    if occupies(t.status):  # running or held: a session runs
        word = _session_word(t, word, tabs, tabs_why)
    return f"{ref}  {t.stage.value}  {word}  {_age(t, now)}"


def _park_word(t: state.TaskState) -> str:
    """Where t is parked, with the reason when a human holds it."""
    fields = read_model.shown(t)
    if fields.park == read_model.PARK_HUMAN and fields.park_reason:
        return f"{fields.park}/{fields.park_reason}"
    return fields.park


def _session_word(t: state.TaskState, word: str, tabs, tabs_why) -> str:
    """The park word of a task whose session runs, completed by its tab: the
    tab's name when live, "no session" when gone, "unknown" when the tabs
    could not be listed."""
    if tabs is None:
        return word or f"unknown ({tabs_why})"
    tab = sessions.session_name(t.repository, t.issue)
    if tab not in tabs:
        return word or "no session"
    return "  ".join(filter(None, [word or "live", tab]))


def _lines(lines: list[str]) -> str:
    """One row: the first line after the label, the rest indented under it."""
    return ("\n" + " " * 11).join(lines) or "none"


def _tasks_row(root, tasks, now, tabs, tabs_why) -> str:
    open_tasks = [t for t in tasks if t.status.kind is not Kind.DONE]
    lines = [_task_line(t, now, tabs, tabs_why) for t in open_tasks]
    lines += [f"{ref}  unknown (unreadable task file)" for ref in _unreadable(root)]
    return _lines(lines)


def _question(t: state.TaskState, now, url: str) -> str | None:
    base = f"{url}/task/{quote(t.repository, safe='')}/{t.issue}" if url else ""
    ref, age = f"{t.repository}#{t.issue}", _age(t, now)
    req = state.operator_request(t)
    if req is not None:
        what = "plan approval" if req.kind == "plan-approval" else "answers"
        return "  ".join(filter(None, [ref, what, req.path, age, base and base + "/review"]))
    if _awaits_decision(t):
        return "  ".join(filter(None, [ref, "decision", f'"{_short(t.status.note)}"',
                                       age, base]))
    return None


def _awaits_decision(t: state.TaskState) -> bool:
    return t.status.kind is Kind.PARKED_HUMAN and t.status.reason is HumanReason.DECISION


def _short(note: str) -> str:
    """The note cut to 60 characters, an ellipsis marking the cut."""
    return note[:59] + "…" if len(note) > 60 else note


def _questions_row(tasks, now, url) -> str:
    live = [t for t in tasks if not ended(t.status)]
    return _lines([q for q in (_question(t, now, url) for t in live) if q])


_ERROR_RE = re.compile(r"Traceback|Error|ERROR|Exception|Failed with result")


def _errors_row(entries, now) -> str:
    """The last 20 warning-or-worse or error-looking lines of the past 6 h."""
    since = now - timedelta(hours=6)
    hits = [e for e in entries
            if e.time >= since and e.unit.startswith("agent-ops-")
            and (e.priority <= 4 or _ERROR_RE.search(e.message))]
    return _lines([f"{_hhmm(e.time, now)}  {e.unit}  {e.message[:200]}"
                   for e in hits[-20:]])


def _or_unknown(value, why, render, what="") -> str:
    return render(value) if value is not None else f"unknown ({what}{why})"


def _guard(probe: Callable):
    """(value, None) or (None, why): a source that fails degrades one row."""
    try:
        return probe(), None
    except Exception as e:  # noqa: BLE001 — any probe failure is "unknown"
        return None, str(e) or type(e).__name__


def run(config_path: str, now: datetime, probes: Probes, out: TextIO) -> int:
    cfg = config.load_config(config_path)
    root = Path(cfg.state_dir)
    units, units_why = _guard(probes.units)
    tabs, tabs_why = _guard(probes.tabs)
    journal, journal_why = _guard(probes.journal)
    tasks = state.load_all(root)
    hb = _json(root / "pass.json")
    marker = _json(root / "auth-dark")
    request = _time((_json(root / triage.REQUEST_FILE) or {}).get("requested_at"))
    usages = _usages(cfg, root, now)
    triage_tab = tabs is not None and triage.TAB_LABEL in tabs
    verdict = _verdict(cfg, root, now, units, tabs, tasks, hb, usages, marker)
    if tabs is None:
        verdict += f" · herdr unreachable ({tabs_why})"
    names = {p.name: p.paused for p in cfg.projects}
    rows = [
        ("dispatcher", _dispatcher_row(root, hb, units, now)),
        ("units", _or_unknown(units, units_why, lambda u: _units_row(u, now))),
        ("projects", " · ".join(f"{n}: {'PAUSED' if p else 'active'}"
                                for n, p in names.items())),
        ("holds", _holds(root, request, marker, now)),
        ("slots", f"{_used(tasks, triage_tab)}/{cfg.capacity}"),
        ("tasks", _tasks_row(root, tasks, now, tabs, tabs_why)),
        ("questions", _questions_row(tasks, now, cfg.console_url)),
        ("errors", _or_unknown(journal, journal_why, lambda j: _errors_row(j, now),
                                "journalctl failed: ")),
    ]
    print(f"VERDICT  {verdict}", file=out)
    for label, text in rows:
        print(f"{label:<11}{text}", file=out)
    return 0
