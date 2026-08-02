"""The I/O edge. Every filesystem/subprocess/network read or write the web
service performs lives here, so routes and read_model stay pure and tests
fake exactly one seam."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dispatcher import budget, eventlog, queue_ops, state, triage
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.intents import write_intent
from dispatcher.queue_ops import QueuePlan
from dispatcher.state import TaskState

RANK_TTL_SECONDS = 15.0
DESCRIPTION_TTL_SECONDS = 300.0
QUARANTINE_TTL_SECONDS = 30.0
# The board's hard bound on quarantine resolution: at most this many NEW `gh`
# probes started per request, none of them inline, and at most this many
# seconds of total waiting before the request answers with what it has.
QUARANTINE_MAX_PROBES = 4
QUARANTINE_WAIT_SECONDS = 2.0
DISPATCHER_KICK = ("systemctl", "--user", "start",
                   "agent-ops-dispatcher.service")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class _Probe:
    """One in-flight off-request blocker-state lookup. Deliberately not a
    concurrent.futures Future: ThreadPoolExecutor joins its threads at
    interpreter exit, so a `gh` call wedged for its full 120 s timeout would
    hold up shutdown. A daemon thread plus an Event never does."""
    done: threading.Event = field(default_factory=threading.Event)
    value: bool | None = None


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
        self._desc_cache: dict[tuple[str, int], dict] = {}
        # (blocker repo, issue) -> (resolved_at, open?) written incrementally
        # by each probe as it lands, so a slow batch still leaves its finished
        # results behind for the next request instead of caching nothing.
        self._blocker_cache: dict[tuple[str, int],
                                  tuple[float, bool | None]] = {}
        self._blocker_probes: dict[tuple[str, int], _Probe] = {}

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

    def issue_description(self, repo: str, number: int) -> dict:
        """gh-backed issue body with a TTL cache. A fetch failure serves the
        last cached value if one exists (stale beats blank, cf. rank_rows);
        cold-cache failure returns an explicit error payload, never raises."""
        now = self._clock()
        key = (repo, number)
        cached = self._desc_cache.get(key)
        if cached and now - cached["at"] < DESCRIPTION_TTL_SECONDS:
            return cached["data"]
        try:
            d = self._github.issue_view(repo, number)
            data = {"title": str(d.get("title") or ""),
                    "body": str(d.get("body") or ""),
                    "url": str(d.get("url") or ""),
                    "fetched_at": _iso(now), "error": ""}
        except Exception as exc:
            if cached:
                return cached["data"]
            return {"title": "", "body": "", "url": "",
                    "fetched_at": _iso(now), "error": str(exc)}
        self._desc_cache[key] = {"data": data, "at": now}
        return data

    def usage(self) -> UsageSnapshot:
        return budget.fetch_usage(self._cfg.state_dir, now=self._clock)

    def quarantine_entries(self) -> list[dict]:
        """Every quarantine record, parsed once for both /api/failures and the
        board's claim forecast.

        Keyed the way failures.check_quarantine keys them: by FILENAME
        (`{target}-{issue}.json`), which is the only thing a lookup can match
        — a record whose body disagrees with its name must not shift the key,
        or the board would hide a candidate the dispatcher WOULD claim. Names
        that do not fit the scheme keep the old lenient handling: no lookup can
        ever match them, so they are reported but never blocking (`keyed`).

        A record whose body is unreadable or is not a JSON object is still
        returned, flagged `readable=False`, when its name is keyable — that is
        the case check_quarantine treats as still blocked, and dropping it here
        would tell the board the candidate is claimable. Blank defaults keep
        FailuresView's model valid for such a record.

        The read is guarded with (OSError, ValueError), not
        (json.JSONDecodeError, OSError): a torn write_text yields invalid UTF-8
        and raises UnicodeDecodeError, a ValueError — the same widening
        pass_heartbeat and events_tail already carry. This route may never 500.
        """
        root = self.state_dir / "quarantine"
        entries = []
        for p in sorted(root.glob("*.json")) if root.exists() else []:
            target, _, raw = p.stem.rpartition("-")
            try:
                keyed_issue: int | None = int(raw)
            except ValueError:
                keyed_issue = None
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                d = None
            if not isinstance(d, dict):
                # Unreadable, or valid JSON that is not an object (null, a
                # list): rec.get() would raise. Only worth reporting when a
                # lookup could match the name.
                if keyed_issue is None:
                    continue
                d, readable = {}, False
            else:
                readable = True
            entry = {"task_issue": 0, "blocker_repo": "", "blocker_issue": 0,
                     "fingerprint": "", "created_at": "", **d}
            entry["target"] = (target if keyed_issue is not None
                               else p.stem.rsplit("-", 1)[0])
            if keyed_issue is not None:
                entry["task_issue"] = keyed_issue
            entry["keyed"] = keyed_issue is not None
            entry["readable"] = readable
            entries.append(entry)
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

    def pass_heartbeat(self) -> dict | None:
        try:
            d = json.loads((self.state_dir / "pass.json").read_text())
        except (OSError, ValueError):
            # OSError: file missing or unreadable.
            # ValueError (incl. JSONDecodeError): torn write mid-UTF-8 or
            # invalid JSON — pass.json is rewritten every dispatcher pass,
            # so a partial read is realistic (cf. _read_snapshot same idiom).
            return None
        return d if isinstance(d, dict) else None

    def triage_state(self) -> tuple[bool, bool]:
        """(claims_paused, triage_running) from exactly ONE tmux probe.

        Mirrors dispatcher/main.py:1135-1137, which evaluates triage.running()
        once per pass for the reason its comment gives: running() is a real
        subprocess with timeout=30. triage.pending() calls running() itself, so
        asking sources for the two signals separately ran it twice per board
        request — a wedged tmux server stalled the hottest route for up to 60 s
        per connected client. Composed here from the same primitives instead.

        Degrades to (False, False) on any error: False is the non-blocking
        value for both. A read failure must not make the console claim claims
        are paused, nor reduce the effective capacity it shows.
        """
        try:
            running = triage.running()
        except Exception:
            running = False
        try:
            # pending() == request file present OR running — inlined so the
            # running half is the value already probed above.
            paused = running or triage.load_request(self.state_dir) is not None
        except Exception:
            paused = running
        return paused, running

    def _blocker_probe(self, key: tuple[str, int]) -> _Probe:
        """Start (or join) an OFF-REQUEST probe of one blocker issue's state.

        `gh issue view` runs through github._run with timeout=120, so this must
        never be awaited inline: N records would serialise into N x 120 s on
        /api/board. The probe runs on a daemon thread (no interpreter-exit
        join, unlike a ThreadPoolExecutor) and writes its cache entry as soon
        as it lands, so a slow batch still leaves every finished result behind
        for the next request. Keyed in-flight so concurrent SSE-driven clients
        share one probe instead of each starting their own.
        """
        existing = self._blocker_probes.get(key)
        if existing is not None:
            return existing
        probe = _Probe()
        self._blocker_probes[key] = probe

        def run() -> None:
            try:
                probe.value = self.issue_open(*key)  # degrades to None itself
                self._blocker_cache[key] = (self._clock(), probe.value)
            finally:
                self._blocker_probes.pop(key, None)
                probe.done.set()

        threading.Thread(target=run, daemon=True,
                         name=f"blocker-probe-{key[0]}#{key[1]}").start()
        return probe

    def quarantined(self) -> frozenset[tuple[str, int]]:
        """(target, issue) pairs _claim_new would skip, mirroring
        failures.check_quarantine — read-only: the web service never deletes a
        dispatcher record, it only predicts.

        Semantics copied from failures.py:160: a record blocks until its
        blocker issue is CLOSED, and every uncertainty resolves to "still
        blocked" (unreadable record, missing blocker_issue, unknown blocker
        state, a probe that has not landed yet) exactly as check_quarantine
        does — so the forecast never claims an issue the dispatcher will skip.
        Records parse through quarantine_entries(), which keys them on the
        filename exactly as check_quarantine looks them up.

        BOUNDED: this sits on /api/board, refetched on every SSE tick, and the
        underlying `gh` call can block for 120 s. Per request it starts at most
        QUARANTINE_MAX_PROBES new probes, never inline, and waits at most
        QUARANTINE_WAIT_SECONDS in TOTAL for results — so the board's added
        latency is capped at that one number regardless of record count, gh
        latency, or how many clients are connected. Anything unresolved at the
        deadline is reported blocked for this request and picked up from cache
        on a later one.
        """
        out: set[tuple[str, int]] = set()
        waiting: list[tuple[tuple[str, int], _Probe]] = []
        started = 0
        for rec in self.quarantine_entries():
            if not rec["keyed"]:
                continue  # no lookup can match this name; never blocking
            key = (rec["target"], rec["task_issue"])
            if not rec["readable"] or not rec["blocker_issue"]:
                out.add(key)  # unreadable / no blocker: a human must clear it
                continue
            bkey = (rec["blocker_repo"], rec["blocker_issue"])
            cached = self._blocker_cache.get(bkey)
            if (cached is not None
                    and self._clock() - cached[0] < QUARANTINE_TTL_SECONDS):
                if cached[1] is False:
                    continue  # blocker confirmed closed: claimable next pass
                out.add(key)
                continue
            in_flight = bkey in self._blocker_probes
            if not in_flight and started >= QUARANTINE_MAX_PROBES:
                out.add(key)  # over the per-request cap: blocked for now
                continue
            if not in_flight:
                started += 1
            waiting.append((key, self._blocker_probe(bkey)))
        deadline = time.monotonic() + QUARANTINE_WAIT_SECONDS
        for key, probe in waiting:
            if not probe.done.wait(max(0.0, deadline - time.monotonic())):
                out.add(key)  # not landed in time: blocked, retried next tick
            elif probe.value is not False:
                out.add(key)  # open, or unknown (issue_open degraded to None)
        return frozenset(out)

    def events_tail(self, limit: int) -> list[dict]:
        # Degrade to [] rather than 500: an unreadable event log costs
        # duration/timeline data but must never break /api/board or
        # /api/task/{issue}. A torn append (UnicodeDecodeError -> ValueError)
        # is realistic because the dispatcher appends under normal operation.
        try:
            return eventlog.read_tail(self._cfg.state_dir, limit=limit)
        except (OSError, ValueError):
            return []

    def pane_tail(self, issue: int) -> str:
        try:
            if self._sessions.is_alive(issue):
                return self._sessions.capture_tail(issue)
        except Exception:
            # capture runs tmux with timeout=30, which RAISES on a wedged
            # server. A missing tail degrades the task view; it must never
            # 500 it (cf. issue_open degrading to None).
            return ""
        return self._read_snapshot(issue)

    def pane_history(self, issue: int, lines: int = 2000) -> str:
        try:
            if self._sessions.is_alive(issue):
                return self._sessions.capture_history(issue, lines)
        except Exception:
            # Same degrade contract as pane_tail: a wedged tmux server
            # (capture_history runs with timeout=30 and RAISES) must never
            # 500 the history view.
            return ""
        return self._read_snapshot(issue)

    def _read_snapshot(self, issue: int) -> str:
        """Dead session: serve the scrollback Sessions.end() persisted.
        Missing/unreadable file (pre-feature parks) degrades to ''."""
        try:
            return (self.state_dir / "snapshots"
                    / f"task-{issue}.txt").read_text()
        except (OSError, ValueError):
            # OSError: file missing or unreadable.
            # ValueError: includes UnicodeDecodeError when snapshot is corrupt/truncated mid-UTF-8.
            return ""

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
            + list(root.glob("attached-*")) + [root / "pass.json"])
        budget_d = digest([root / "usage-cache.json",
                           root / "budget-stalled"])
        failures = digest(
            (list((root / "failures").iterdir())
             if (root / "failures").exists() else [])
            + (list((root / "quarantine").iterdir())
               if (root / "quarantine").exists() else []))
        events = root / "events.jsonl"
        history = str(events.stat().st_size) if events.exists() else "0"
        # Note: "board" and "queue" share the same digest (an intentional
        # pre-existing alias). Adding pass.json to the board list means a
        # completed dispatcher pass also invalidates the "queue" SSE key.
        # This is harmless — the 15 s rank_rows TTL absorbs the extra ping —
        # but it was not deliberate coupling; recorded here for future readers.
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
