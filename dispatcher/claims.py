"""Which target the next free capacity unit goes to. Shared by the
dispatcher's claim round and the console's next-claim forecast, so the two
can never disagree about the order."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from dispatcher.eventlog import EVENTS_FILE


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
            last[target] = max(last.get(target, ""), ts)
    return last
