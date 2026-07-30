"""Daily backlog triage: request/cursor state, repo enumeration, and (in
later tasks) the sweep runner. The systemd timer enqueues a request; the
dispatcher pass launches the sweep in a detached tmux session named
`triage`, whose liveness is the capacity signal."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.config import Config

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
