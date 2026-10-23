"""Which target the next free capacity unit goes to. Shared by the
dispatcher's claim round and the console's next-claim forecast, so the two
can never disagree about the order."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from dispatcher.eventlog import EVENTS_FILE
from dispatcher.state import TERMINAL_STAGES, TaskState, active, load_all


# The slice's fixed number of places: `max_open` may be set higher, but never
# raises the project or unfinished-task limit.
PLACES = 10


def box_free(capacity: int, tasks: list[TaskState], triage_running: bool,
             held: int = 0) -> int:
    """Free capacity across the whole box. `tasks` must be every task, not
    one target's: capacity is one number shared by all targets. A running
    triage sweep holds a unit that active() cannot see (a herdr tab, not a
    TaskState), as does each `held` verification or still-stopping task."""
    return capacity - len(active(tasks)) - int(triage_running) - held


def open_tasks(tasks: list[TaskState]) -> list[TaskState]:
    """Every task not yet finished: running, parked, awaiting review or CI,
    pr-open. Each one keeps a worktree on disk."""
    return [t for t in tasks if t.stage not in TERMINAL_STAGES]


def claim_room(capacity: int, max_open: int, tasks: list[TaskState],
               triage_running: bool, held: int = 0) -> int:
    """How many NEW issues this pass may claim: free capacity, further
    capped so the box never holds more than `max_open` unfinished tasks.
    Woken work and PR feedback are not claims and ignore `max_open`; only
    the claim round and the console's forecast go through here."""
    return min(box_free(capacity, tasks, triage_running, held),
               max_open - len(open_tasks(tasks)))


def held_reservations(state_dir: str | Path) -> list[str]:
    """Projects whose enable request was admitted and neither failed nor
    superseded: each holds its place through the PR / deployment wait and past
    it, until the dispatcher's own configuration lists the project enabled
    (then it counts as enabled). Written by project_control as `held`."""
    ops = (json.loads(p.read_text()) for p in (Path(state_dir) / "operations").glob("*.json"))
    return [op["plan"]["name"] for op in ops if op.get("held")]


def places_taken(tasks: list[TaskState], projects) -> int:
    """Unfinished tasks plus one reserved place per project with none."""
    open_ = open_tasks(tasks)
    busy = {t.target for t in open_}
    return len(open_) + sum(k not in busy for k in set(projects))


def configuration_fits(state_dir: str | Path, enabled: list[str]) -> bool:
    """Whether a configuration enabling `enabled` leaves every unfinished task and
    held reservation a place. The updater calls it under the convergence lock
    before it applies a configuration; it reads the state the claim round reads."""
    projects = {*enabled, *held_reservations(state_dir)}
    return (len(projects) <= PLACES
            and places_taken(load_all(state_dir), projects) <= PLACES)


def reservation_denial(owner: str, tasks: list[TaskState], enabled: list[str],
                       max_open: int, holds: list[str] = ()) -> str:
    """Why `owner` may not claim one more task, or "" when it may. Every
    enabled project holds one place while it has no unfinished task; the rest
    are shared. Paused projects hold none, but their unfinished tasks count.
    A legacy target holds none, so it always needs a shared place. `holds`
    are admitted enable requests not yet applied: they hold a place like an
    enabled project. Returns "total-limit" or "reserved-places"."""
    max_open = min(max_open, PLACES)
    open_ = open_tasks(tasks)
    if len(open_) >= max_open:
        return "total-limit"
    busy = {t.target for t in open_}
    if owner in enabled and owner not in busy:
        return ""  # its own reservation
    return "reserved-places" if places_taken(tasks, [*enabled, *holds]) >= max_open else ""


def pick_target(names: list[str], active_counts: Mapping[str, int],
                last_claimed: Mapping[str, str],
                max_active: Mapping[str, int | None]) -> str | None:
    """Fewest active among targets under their `max_active`, ties to the
    least recent claim ("" = never claimed = oldest), then list order (min is
    stable). None when every target is capped."""
    open_ = [n for n in names
             if max_active.get(n) is None or active_counts[n] < max_active[n]]
    return min(open_, key=lambda n: (active_counts[n], last_claimed.get(n, "")),
               default=None)


def last_claims(state_dir: str | Path) -> dict[str, str]:
    """Each target's latest `claimed` ts in events.jsonl. Rotation drops old
    history; a target that then looks never-claimed only loses a tie."""
    p = Path(state_dir) / EVENTS_FILE
    if not p.exists():
        return {}
    last: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        if '"claimed"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict) and e.get("event") == "claimed":
            target, ts = e.get("target", ""), e.get("ts", "")
            if isinstance(target, str) and isinstance(ts, str):
                last[target] = max(last.get(target, ""), ts)
    return last
