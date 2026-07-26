"""The I/O edge. Every filesystem/subprocess/network read or write the web
service performs lives here, so routes and read_model stay pure and tests
fake exactly one seam."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dispatcher import budget, eventlog, queue_ops, state
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.intents import write_intent
from dispatcher.queue_ops import QueuePlan
from dispatcher.state import TaskState

RANK_TTL_SECONDS = 15.0
DISPATCHER_KICK = ("systemctl", "--user", "start",
                   "agent-ops-dispatcher.service")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class Sources:
    def __init__(self, cfg: Config, sessions, github,
                 clock: Callable[[], float] = time.time,
                 systemctl: tuple[str, ...] = DISPATCHER_KICK):
        self._cfg = cfg
        self._sessions = sessions
        self._github = github
        self._clock = clock
        self._systemctl = systemctl
        self._rank_cache: dict[str, dict] = {}

    @property
    def state_dir(self) -> Path:
        return Path(self._cfg.state_dir)

    # -- reads -----------------------------------------------------------

    def tasks(self) -> list[TaskState]:
        return state.load_all(self._cfg.state_dir)

    def rank_rows(self, target: Target) -> tuple[list[dict], str, bool]:
        now = self._clock()
        cached = self._rank_cache.get(target.name)
        if cached and now - cached["fetched_at"] < RANK_TTL_SECONDS:
            return cached["rows"], cached["as_of"], False
        try:
            rows = self._github.rank_rows(target)
        except Exception:
            if cached:  # never a blank queue if a cache ever existed
                return cached["rows"], cached["as_of"], True
            return [], _iso(now), True
        self._rank_cache[target.name] = {
            "rows": rows, "fetched_at": now, "as_of": _iso(now)}
        return rows, _iso(now), False

    def usage(self) -> UsageSnapshot:
        return budget.fetch_usage(self._cfg.state_dir, now=self._clock)

    def quarantine_entries(self) -> list[dict]:
        root = self.state_dir / "quarantine"
        entries = []
        for p in sorted(root.glob("*.json")) if root.exists() else []:
            try:
                d = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            d["target"] = p.stem.rsplit("-", 1)[0]
            entries.append(d)
        return entries

    def fingerprint_entries(self) -> list[dict]:
        root = self.state_dir / "failures"
        entries = []
        for p in sorted(root.iterdir()) if root.exists() else []:
            try:
                d = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            entries.append({"fingerprint": p.name, **d})
        return entries

    def issue_open(self, repo: str, number: int) -> bool | None:
        try:
            return self._github.issue_state(repo, number) == "OPEN"
        except Exception:
            return None  # unknown, surfaced as such — never a guess

    def events_tail(self, limit: int) -> list[dict]:
        return eventlog.read_tail(self._cfg.state_dir, limit=limit)

    def pane_tail(self, issue: int) -> str:
        return self._sessions.capture_tail(issue)

    def session_alive(self, issue: int) -> bool:
        return self._sessions.is_alive(issue)

    def pending_intents(self) -> list[dict]:
        root = self.state_dir / "intents"
        out = []
        for p in sorted(root.glob("*.json")) if root.exists() else []:
            try:
                d = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            out.append({"action": d.get("action", ""),
                        "issue": d.get("issue", 0),
                        "actor": d.get("actor", ""),
                        "created_at": d.get("created_at", "")})
        return out

    def state_fingerprint(self) -> str:
        root = self.state_dir

        def digest(paths) -> str:
            h = hashlib.sha256()
            for p in sorted(paths):
                try:
                    st = p.stat()
                except OSError:
                    continue
                h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size};".encode())
            return h.hexdigest()[:16]

        board = digest(
            list(root.glob("task-*.json")) + list(root.glob("waiting-*"))
            + list(root.glob("attached-*")))
        budget_d = digest([root / "usage-cache.json",
                           root / "budget-stalled"])
        failures = digest(
            (list((root / "failures").iterdir())
             if (root / "failures").exists() else [])
            + (list((root / "quarantine").iterdir())
               if (root / "quarantine").exists() else []))
        events = root / "events.jsonl"
        history = str(events.stat().st_size) if events.exists() else "0"
        return json.dumps({"board": board, "queue": board,
                           "budget": budget_d, "failures": failures,
                           "history": history}, sort_keys=True)

    def has_attached(self, issue: int) -> bool:
        return state.has_attached(self._cfg.state_dir, issue)

    # -- writes ----------------------------------------------------------

    def submit_intent(self, action: str, issue: int, payload: dict,
                      actor: str) -> str:
        path = write_intent(self._cfg.state_dir, action, issue, payload,
                            actor, int(self._clock() * 1000))
        try:  # best-effort kick; the 10-minute timer is the floor
            subprocess.run(self._systemctl, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"dispatcher kick failed (non-fatal): {e}")
        return path.name

    def apply_queue_plan(self, target: Target, issue: int,
                         plan: QueuePlan) -> None:
        queue_ops.apply_plan(self._github, target, issue, plan)

    def append_event(self, event: str, *, target: str = "", issue: int = 0,
                     actor: str = "", detail: str = "") -> None:
        eventlog.append_event(self._cfg.state_dir, event, target=target,
                              issue=issue, actor=actor, detail=detail)

    def mark_attached(self, issue: int) -> None:
        state.mark_attached(self._cfg.state_dir, issue)

    def clear_attached(self, issue: int) -> None:
        state.clear_attached(self._cfg.state_dir, issue)
