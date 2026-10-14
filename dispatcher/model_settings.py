"""The `models:` block the console's settings page stores
(`<state_dir>/model-settings.json`). When present it replaces the `models:`
block of targets.yaml for the dispatcher, triage and the console; deleting it
brings the targets.yaml block back. A target's own `models:` block still
replaces both (config.policy_for)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.state import write_json_atomic

FILE = "model-settings.json"


def load(state_dir: str | Path) -> dict | None:
    """The stored document {"models", "set_by", "set_at"}, or None when there
    is none. Raises ValueError when the file is unreadable or holds no
    `models` mapping, so the caller can say why it was not applied."""
    path = Path(state_dir) / FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{path} is unreadable: {e}") from e
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{path} is not JSON: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict) or not doc["models"]:
        raise ValueError(f"{path} holds no models mapping")
    return doc


def save(state_dir: str | Path, models: dict, *, actor: str, now: datetime) -> None:
    write_json_atomic(Path(state_dir) / FILE, {
        "models": models, "set_by": actor,
        "set_at": now.astimezone(timezone.utc).isoformat()})


def clear(state_dir: str | Path) -> None:
    (Path(state_dir) / FILE).unlink(missing_ok=True)
