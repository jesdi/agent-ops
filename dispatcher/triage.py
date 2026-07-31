"""Daily backlog triage: request/cursor state, repo enumeration, and (in
later tasks) the sweep runner. The systemd timer enqueues a request; the
dispatcher pass launches the sweep in a detached tmux session named
`triage`, whose liveness is the capacity signal."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dispatcher import containers, triage_apply, triage_prefetch
from dispatcher.budget import fetch_usage, should_spawn
from dispatcher.config import Config
from dispatcher.prompts import render_triage_prompt

REQUEST_FILE = "triage-request.json"
CURSORS_FILE = "triage_cursors.json"
TRIAGE_DIR = "triage"
TMUX_SESSION = "triage"
ACQUIRE_TIMEOUT_SECONDS = 2 * 3600
SESSION_TIMEOUT_SECONDS = 20 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    delta = (datetime.fromisoformat(now_iso)
             - datetime.fromisoformat(requested_at))
    return delta.total_seconds() > ACQUIRE_TIMEOUT_SECONDS


def load_cursors(state_dir: str | Path) -> dict[str, str]:
    p = Path(state_dir) / CURSORS_FILE
    if not p.exists():
        return {}
    try:
        return {str(k): str(v) for k, v in json.loads(p.read_text()).items()}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return {}


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
    fname = f"{repo.replace('/', '-')}-{started_date}.json"
    decisions_path = triage_dir / fname
    decisions_path.unlink(missing_ok=True)
    prompt = render_triage_prompt({
        "repo": repo,
        "decisions_path": f"/triage/{fname}",
        "context_json": json.dumps(blob, indent=2),
    })
    model = cfg.triage_model or cfg.models.default
    name = _session_name(repo)
    cmd = containers.triage_cmd(name, _clone_for(cfg, repo),
                                str(triage_dir), cfg.session_memory,
                                cfg.session_cpus, model, prompt)
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
    started = _now()
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
    deps.notifier.send("triage_report", lines=lines)


def guarded_sweep(cfg: Config, deps) -> None:
    try:
        run_sweep(cfg, deps)
    except Exception as e:  # noqa: BLE001 — notify, then let systemd see it
        deps.notifier.send("triage_report", lines=[f"sweep crashed: {e}"])
        raise
