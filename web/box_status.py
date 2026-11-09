"""`agent-ops status`: a read-only report of the box from local state alone.

Interface only; the behavior is not written yet."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, NamedTuple, TextIO


class Unit(NamedTuple):
    state: str          # systemd ActiveState: active, inactive, failed, ...
    since: datetime     # when it entered that state


@dataclass(frozen=True)
class Probes:
    """Every outside source the report reads; each one only queries.
    units: every `agent-ops-*` user unit and timer, by name.
    tabs: the live herdr tab labels."""
    units: Callable[[], dict[str, Unit]]
    tabs: Callable[[], set[str]]


def run(config_path: str, now: datetime, probes: Probes, out: TextIO) -> int:
    raise NotImplementedError("agent-ops status is not written yet")
