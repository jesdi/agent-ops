"""Which target the next free capacity unit goes to. Shared by the
dispatcher's claim round and the console's next-claim forecast, so the two
can never disagree about the order."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from dispatcher.eventlog import EVENTS_FILE
from dispatcher.state import TERMINAL_STAGES, TaskState, active


# The slice's fixed number of places: `max_open` may be set higher, but never
# raises the project or unfinished-task limit.
PLACES = 10


def box_free(capacity: int, tasks: list[TaskState], triage_running: bool) -> int:
    """Free capacity across the whole box. `tasks` must be every task, not
    one target's: capacity is one number shared by all targets. A running
    triage sweep holds a unit that active() cannot see (a herdr tab, not a
    TaskState)."""
    return capacity - len(active(tasks)) - int(triage_running)


def open_tasks(tasks: list[TaskState]) -> list[TaskState]:
    """Every task not yet finished: running, parked, awaiting review or CI,
    pr-open. Each one keeps a worktree on disk."""
    return [t for t in tasks if t.stage not in TERMINAL_STAGES]


def claim_room(capacity: int, max_open: int, tasks: list[TaskState],
               triage_running: bool) -> int:
    """How many NEW issues this pass may claim: free capacity, further
    capped so the box never holds more than `max_open` unfinished tasks.
    Woken work and PR feedback are not claims and ignore `max_open`; only
    the claim round and the console's forecast go through here."""
    return min(box_free(capacity, tasks, triage_running),
               max_open - len(open_tasks(tasks)))


def reservation_denial(owner: str, tasks: list[TaskState], enabled: list[str],
                       max_open: int) -> str:
    """Why `owner` may not claim one more task, or "" when it may. Every
    enabled project holds one place while it has no unfinished task; the rest
    are shared. Paused projects hold none, but their unfinished tasks count.
    A legacy target holds none, so it always needs a shared place. Returns
    "total-limit" or "reserved-places"."""
    max_open = min(max_open, PLACES)
    open_ = open_tasks(tasks)
    if len(open_) >= max_open:
        return "total-limit"
    busy = {t.target for t in open_}
    if owner in enabled and owner not in busy:
        return ""  # its own reservation
    held = sum(k not in busy for k in enabled)
    return "reserved-places" if len(open_) + held >= max_open else ""


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
