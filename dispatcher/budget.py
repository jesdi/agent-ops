"""Spawn/no-spawn gatekeeper for every stage spawn (v1).

Research feeding this module: portfolio_eval#155."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageSnapshot:
    utilization: float       # 0..1 of the 5-hour session window
    minutes_to_reset: float
    source: str              # "oauth" | "ccusage" | "unavailable"


def should_spawn(
    u: UsageSnapshot,
    threshold: float,
    racing_minutes: int,
    racing_threshold: float,
) -> bool:
    if u.source == "unavailable":
        return False
    # Reset-racing bonus: window resets soon → relax the ceiling so the
    # remainder of the window isn't wasted.
    limit = racing_threshold if u.minutes_to_reset <= racing_minutes else threshold
    return u.utilization < limit
