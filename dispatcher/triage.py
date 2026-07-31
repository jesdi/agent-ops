"""Daily backlog triage: request/cursor state, repo enumeration, and the
sweep runner. The systemd timer enqueues a request; the
dispatcher pass launches the sweep in a detached tmux session named
`triage`, whose liveness is the capacity signal."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import containers, triage_apply, triage_prefetch
from dispatcher.budget import fetch_usage, should_spawn
from dispatcher.config import Config
from dispatcher.prompts import render_triage_prompt
from dispatcher.state import active, load_all

REQUEST_FILE = "triage-request.json"
CURSORS_FILE = "triage_cursors.json"
TRIAGE_DIR = "triage"
TMUX_SESSION = "triage"
ACQUIRE_TIMEOUT_SECONDS = 2 * 3600
SESSION_TIMEOUT_SECONDS = 20 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cursor_now() -> str:
    """Now as a GitHub *search* timestamp: whole seconds, `Z` suffix.

    Cursors are interpolated into `--search "updated:>{cursor}"`. GitHub's
    search date grammar accepts `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS±HH:MM`
    only — fractional seconds are not in it, and an unparseable date
    qualifier is not an error: GitHub degrades it to free text, so the query
    matches ~nothing and every repo silently reports "nothing new". Same
    second-granularity lesson as main._cursor_now(), different consumer.

    Stepped back one second for the same reason main does: the qualifier is a
    strict `updated:>`, so an issue touched during the second the sweep
    started would otherwise fall outside tomorrow's window too and never be
    triaged. A one-second overlap can at worst re-triage an issue touched in
    that exact second — a redundant look beats a silent miss."""
    return ((datetime.now(timezone.utc).replace(microsecond=0)
             - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _normalize_cursor(value: str) -> str | None:
    """Coerce a stored cursor into the search-safe shape, or None if it is
    not a timestamp at all. Cursors written before the shape was fixed carry
    microseconds; rewriting them on load is what keeps those repos from
    querying with a qualifier GitHub silently ignores. An uninterpretable
    cursor is dropped rather than passed through — the repo then re-seeds
    (triaging nothing once) instead of degrading to free-text search
    forever."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc).replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def running() -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True, timeout=30).returncode == 0


def load_request(state_dir: str | Path) -> str | None:
    p = Path(state_dir) / REQUEST_FILE
    if not p.exists():
        return None
    try:
        return str(json.loads(p.read_text())["requested_at"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def enqueue(state_dir: str | Path) -> bool:
    if running() or load_request(state_dir) is not None:
        return False
    p = Path(state_dir) / REQUEST_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"requested_at": _now()}, indent=2))
    tmp.replace(p)
    return True


def clear_request(state_dir: str | Path) -> None:
    (Path(state_dir) / REQUEST_FILE).unlink(missing_ok=True)


def pending(state_dir: str | Path) -> bool:
    return load_request(state_dir) is not None or running()


def expired(requested_at: str, now_iso: str) -> bool:
    """Fail closed on a timestamp that won't parse: a truncated or
    hand-edited request file must expire (tick then clears it), not raise out
    of every dispatcher pass and have guarded_pass file a pass-crash issue
    until someone deletes the file by hand. Same idiom as main._grace_elapsed
    and main._flush_done."""
    try:
        delta = (datetime.fromisoformat(now_iso)
                 - datetime.fromisoformat(requested_at))
    except (TypeError, ValueError):
        return True
    return delta.total_seconds() > ACQUIRE_TIMEOUT_SECONDS


def load_cursors(state_dir: str | Path) -> dict[str, str]:
    p = Path(state_dir) / CURSORS_FILE
    if not p.exists():
        return {}
    try:
        raw = {str(k): str(v) for k, v in json.loads(p.read_text()).items()}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {}
    normalized = {k: _normalize_cursor(v) for k, v in raw.items()}
    return {k: v for k, v in normalized.items() if v is not None}


def save_cursors(state_dir: str | Path, cursors: dict[str, str]) -> None:
    p = Path(state_dir) / CURSORS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True))
    tmp.replace(p)


def triage_repos(cfg: Config) -> list[str]:
    repos = [t.repo for t in cfg.targets]
    if cfg.infra_repo and cfg.infra_repo not in repos:
        repos.append(cfg.infra_repo)
    return repos


class SweepError(Exception):
    pass


def _clone_for(cfg: Config, repo: str) -> str:
    for t in cfg.targets:
        if t.repo == repo:
            return t.clone_path
    return str(Path.home() / "agent-ops")


def _session_name(repo: str) -> str:
    return "triage-" + repo.replace("/", "-")


def _run_session(cfg: Config, repo: str, blob: dict, started_date: str,
                 run=subprocess.run) -> dict:
    triage_dir = Path(cfg.state_dir) / TRIAGE_DIR
    triage_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{repo.replace('/', '-')}-{started_date}"
    fname = f"{stem}.json"
    decisions_path = triage_dir / fname
    decisions_path.unlink(missing_ok=True)
    prompt = render_triage_prompt({
        "repo": repo,
        "decisions_path": f"/triage/{fname}",
        "context_json": json.dumps(triage_prefetch.bound(blob), indent=2),
    })
    # The prompt goes through the /triage mount, never through argv — see
    # containers.triage_cmd for the 128 KiB MAX_ARG_STRLEN reason.
    prompt_name = f"{stem}-prompt.md"
    (triage_dir / prompt_name).write_text(prompt)
    model = cfg.triage_model or cfg.models.default
    name = _session_name(repo)
    cmd = containers.triage_cmd(name, _clone_for(cfg, repo),
                                str(triage_dir), cfg.session_memory,
                                cfg.session_cpus, model,
                                f"/triage/{prompt_name}")
    try:
        proc = run(cmd, capture_output=True, text=True,
                   timeout=SESSION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            run(["podman", "kill", name], capture_output=True, text=True,
                timeout=60)
        except Exception:
            pass
        raise SweepError(f"session timed out after "
                         f"{SESSION_TIMEOUT_SECONDS // 60} min")
    if not decisions_path.exists():
        stderr_tail = (proc.stderr or "")[-300:].strip()
        raise SweepError(
            f"session wrote no decisions file "
            f"(rc={proc.returncode}"
            + (f", stderr: {stderr_tail}" if stderr_tail else "")
            + ")"
        )
    try:
        return json.loads(decisions_path.read_text())
    except json.JSONDecodeError as e:
        raise SweepError(f"decisions file is not valid JSON: {e}") from e


def _summary(repo: str, result: triage_apply.ApplyResult) -> list[str]:
    lines = [f"{repo}: {result.labeled} labeled, {result.comments} "
             f"comment(s), {len(result.closes)} close(s) suggested, "
             f"{len(result.rejected)} rejected"]
    lines += [f"  {c}" for c in result.closes]
    lines += [f"  rejected: {r}" for r in result.rejected]
    return lines


def run_sweep(cfg: Config, deps, run=subprocess.run) -> None:
    clear_request(cfg.state_dir)
    started = _cursor_now()  # the value every advanced cursor is set to
    started_date = started[:10]
    usage = fetch_usage(cfg.state_dir)
    if not should_spawn(usage, cfg.budget_threshold, cfg.racing_minutes,
                        cfg.racing_threshold):
        deps.notifier.send("triage_report", lines=[
            f"skipped — budget gate ({usage.source}: "
            f"{usage.utilization:.0%})"])
        return
    cursors = load_cursors(cfg.state_dir)
    lines: list[str] = []
    for repo in triage_repos(cfg):
        cursor = cursors.get(repo)
        if cursor is None:
            cursors[repo] = started
            lines.append(f"{repo}: cursor seeded, nothing triaged")
            continue
        try:
            blob = triage_prefetch.prefetch(repo, cursor, run=run)
            if not blob["issues"]:
                cursors[repo] = started
                lines.append(f"{repo}: nothing new")
                continue
            decisions = _run_session(cfg, repo, blob, started_date, run=run)
            inventory = frozenset(l["name"] for l in blob["labels"])
            result = triage_apply.apply(repo, decisions, inventory, run=run)
            cursors[repo] = started
            lines.extend(_summary(repo, result))
        except Exception as e:  # noqa: BLE001 — isolate repos from each other
            lines.append(f"{repo}: FAILED — {e}")
    save_cursors(cfg.state_dir, cursors)
    deps.notifier.send("triage_report", lines=lines,
                       triage_dir=str(Path(cfg.state_dir) / TRIAGE_DIR))


def guarded_sweep(cfg: Config, deps) -> None:
    try:
        run_sweep(cfg, deps)
    except Exception as e:  # noqa: BLE001 — notify, then let systemd see it
        deps.notifier.send("triage_report", lines=[f"sweep crashed: {e}"])
        raise


LAUNCH_ENV_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                   "AGENT_OPS_STATE_DIR")


def _launch_env() -> list[str]:
    """`-e VAR=value` args carrying the dispatcher's own environment into the
    sweep's tmux session (tmux ≥ 3.0).

    This is the first place a dispatcher *Python* process is launched via
    tmux — Sessions._launch only ever starts podman, whose credentials arrive
    through mounted volumes. A new tmux session otherwise inherits the tmux
    *server's* environment, fixed whenever that server first started: if the
    server was started by anything other than a dispatcher pass under
    `op run` (an operator's interactive `tmux`, agent-ops-web, …) the runner
    has no Telegram credentials and the only symptom is a silent sweep.
    Whichever case a box lands in is nondeterministic across reboots.

    A variable that is unset is simply not forwarded — the runner's own
    fallbacks (containers._state_dir, Notifier's missing-credential warning)
    then apply, exactly as they would in-process."""
    return [arg
            for var in LAUNCH_ENV_VARS
            if os.environ.get(var)
            for arg in ("-e", f"{var}={os.environ[var]}")]


def tick(cfg: Config, deps, config_path: str) -> None:
    """Once per dispatcher pass: expire or launch a pending request. The
    request file survives until the runner consumes it, so claims stay
    paused across the launch gap; tmux-session liveness is the running
    signal (no separate marker to leak on a crash)."""
    if running():
        return
    requested = load_request(cfg.state_dir)
    if requested is None:
        return
    if expired(requested, _now()):
        clear_request(cfg.state_dir)
        deps.notifier.send("triage_report",
                           lines=["skipped — no capacity within 2 h"])
        return
    # Deliberately global, unlike the rest of the dispatcher: _claim_new,
    # _resume_woken and _spawn_feedback all filter by `t.target` first, so
    # `capacity` is per-target there, while this counts every active task
    # across every target against the one number. With a single target (the
    # box's shape) the two agree. With N > 1 targets this is the stricter
    # reading: steady-state active can reach N × capacity, so the sweep would
    # never find a free slot and would expire every morning reporting
    # "skipped — no capacity within 2 h" — a misdiagnosis to recognise rather
    # than debug. Symmetrically, main's `capacity - 1` reduction while the
    # sweep runs subtracts a unit per target. Both directions are safe (the
    # sweep never over-commits the box); the model is deliberately left alone.
    if len(active(load_all(cfg.state_dir))) >= cfg.capacity:
        return  # wait for a natural release; never preempt
    res = subprocess.run(
        ["tmux", "new-session", "-d"] + _launch_env() +
        ["-s", TMUX_SESSION,
         f"{shlex.quote(sys.executable)} -m dispatcher.main "
         f"--config {shlex.quote(config_path)} --triage-run"],
        capture_output=True, timeout=30)
    if res.returncode != 0:
        clear_request(cfg.state_dir)
        deps.notifier.send("triage_report",
                           lines=[f"launch failed: {res.stderr.strip()}"])
