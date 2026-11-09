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

from dispatcher import config, herdr, sessions, state, triage
from dispatcher.claims import box_free
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


def system_probes() -> Probes:
    return Probes(units=_systemd_units, tabs=_herdr_tabs, journal=_journal)


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


def _journal() -> list:
    proc = subprocess.run(
        ["journalctl", "--user", "-u", "agent-ops-*", "--since", "6 hours ago",
         "-o", "json", "--no-pager"],
        capture_output=True, text=True, timeout=20)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
    entries = []
    for line in proc.stdout.splitlines():
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
    if blocker and blocker.name == "dispatcher-down":
        return _stale_heartbeat(units, hb, now)
    timer_down = _timer_down(units)
    if timer_down:
        return timer_down
    if blocker is None:
        return "no local blocker — candidates need GitHub (see console)"
    return f"not claiming: {blocker.name} ({_evidence(blocker.name, gate, usages, marker, request, projects, tasks, cfg, now, triage_tab)})"


def _stale_heartbeat(units, hb, now) -> str:
    """A stale heartbeat while the service is active is a long pass, not a dead dispatcher."""
    service = (units or {}).get(SERVICE)
    if service and service.state == "active":
        return f"dispatcher busy (pass running since {_hhmm(service.since, now)})"
    return f"not claiming: dispatcher-down ({_stale(hb, now)})"


def _timer_down(units) -> str | None:
    """Unknown units (a failed probe) say nothing about the timer."""
    if units is None:
        return None
    timer = units.get(TIMER)
    if timer is not None and timer.state == "active":
        return None
    return f"not claiming: dispatcher-down ({TIMER} {timer.state if timer else 'not found'})"


def _stale(hb: dict | None, now: datetime) -> str:
    finished = _time((hb or {}).get("finished_at"))
    if finished is None:
        return "no pass.json"
    return (f"last pass {_minutes(finished, now)}m ago, "
            f"interval {hb.get('interval_minutes')}m")


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
    return len(state.active(tasks)) + int(triage_tab)


def _dispatcher_row(hb, units, now) -> str:
    finished = _time((hb or {}).get("finished_at"))
    last = (f"last pass {_minutes(finished, now)}m ago, "
            f"interval {hb.get('interval_minutes')}m" if finished else "no pass.json")
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
    word = f"{t.park} {t.park_reason}" if t.park == state.PARK_HUMAN and t.park_reason else t.park
    if state.consumes_capacity(t):  # unparked or parked-login: a session runs
        tab = sessions.session_name(t.repository, t.issue)
        if tabs is None:
            word = word or f"unknown ({tabs_why})"
        else:
            word = "  ".join(filter(None, [word or "live", tab if tab in tabs else ""]))
    return f"{ref}  {t.stage.value}  {word}  {_age(t, now)}"


def _lines(lines: list[str]) -> str:
    """One row: the first line after the label, the rest indented under it."""
    return ("\n" + " " * 11).join(lines) or "none"


def _tasks_row(root, tasks, now, tabs, tabs_why) -> str:
    open_tasks = [t for t in tasks if t.stage != state.Stage.DONE]
    lines = [_task_line(t, now, tabs, tabs_why) for t in open_tasks]
    lines += [f"{ref}  unknown (unreadable task file)" for ref in _unreadable(root)]
    return _lines(lines)


def _question(t: state.TaskState, now, url: str) -> str | None:
    base = f"{url}/task/{quote(t.repository, safe='')}/{t.issue}" if url else ""
    ref, age = f"{t.repository}#{t.issue}", _age(t, now)
    req = t.operator_request
    if req is not None:
        what = "plan approval" if req.kind == "plan-approval" else "answers"
        return "  ".join(filter(None, [ref, what, req.path, age, base and base + "/review"]))
    if t.park == state.PARK_HUMAN and t.park_reason == state.WHY_DECISION:
        note = t.park_note
        note = note[:59] + "…" if len(note) > 60 else note
        return "  ".join(filter(None, [ref, "decision", f'"{note}"', age, base]))
    return None


def _questions_row(tasks, now, url) -> str:
    live = [t for t in tasks if t.stage not in state.TERMINAL_STAGES]
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
        ("dispatcher", _dispatcher_row(hb, units, now)),
        ("units", _units_row(units, now) if units is not None
         else f"unknown ({units_why})"),
        ("projects", " · ".join(f"{n}: {'PAUSED' if p else 'active'}"
                                for n, p in names.items())),
        ("holds", _holds(root, request, marker, now)),
        ("slots", f"{_used(tasks, triage_tab)}/{cfg.capacity}"),
        ("tasks", _tasks_row(root, tasks, now, tabs, tabs_why)),
        ("questions", _questions_row(tasks, now, cfg.console_url)),
        ("errors", _errors_row(journal, now) if journal is not None
         else f"unknown (journalctl failed: {journal_why})"),
    ]
    print(f"VERDICT  {verdict}", file=out)
    for label, text in rows:
        print(f"{label:<11}{text}", file=out)
    return 0
