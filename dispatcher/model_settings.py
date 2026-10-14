"""The `models:` block the console's settings page stores
(`<state_dir>/model-settings.json`). When present it replaces the `models:`
block of targets.yaml for the dispatcher, triage and the console; deleting it
brings the targets.yaml block back. A target's own `models:` block still
replaces both (config.policy_for). config.apply_stored_models applies it."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.state import write_json_atomic

FILE = "model-settings.json"


@dataclass(frozen=True)
class Stored:
    """The block in force is the console's: who stored it, when (ISO 8601)."""
    set_by: str
    set_at: str


@dataclass(frozen=True)
class Rejected:
    """A stored block that is not in force, and why: targets.yaml's runs."""
    reason: str


def _path(state_dir: str | Path) -> Path:
    return Path(state_dir) / FILE


def load(state_dir: str | Path) -> tuple[object, Stored] | None:
    """The stored raw `models:` block (models.parse_block validates it) and
    who stored it, or None when there is none. Raises ValueError when the
    file is unreadable or is not a {"models", ...} JSON object."""
    path = _path(state_dir)
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{path} is unreadable: {e}") from e
    except ValueError as e:
        raise ValueError(f"{path} is not JSON: {e}") from e
    if not isinstance(doc, dict) or "models" not in doc:
        raise ValueError(f"{path} holds no models block")
    return doc["models"], Stored(str(doc.get("set_by") or ""), str(doc.get("set_at") or ""))


def revision(state_dir: str | Path) -> str:
    """What the console's last read saw: a digest of the file, "" when there
    is none. A save or reset sent from an older revision is refused."""
    try:
        return hashlib.sha256(_path(state_dir).read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def save(state_dir: str | Path, models: dict, *, actor: str, now: datetime) -> None:
    write_json_atomic(_path(state_dir), {
        "models": models, "set_by": actor,
        "set_at": now.astimezone(timezone.utc).isoformat()})


def clear(state_dir: str | Path) -> None:
    _path(state_dir).unlink(missing_ok=True)
