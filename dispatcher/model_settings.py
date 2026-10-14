"""The `models:` block the console's settings page stores
(`<state_dir>/model-settings.json`). When present it replaces the `models:`
block of targets.yaml for the dispatcher, triage and the console; deleting it
brings the targets.yaml block back. A target's own `models:` block still
replaces both (config.policy_for). config.apply_stored_models applies it."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dispatcher.state import write_json_atomic

FILE = "model-settings.json"
UNREADABLE = "unreadable"   # the revision of a file that cannot be read


@dataclass(frozen=True)
class Origin:
    """Where the block in force comes from, read with it in one go."""
    revision: str = ""   # digest of the file; "" = no file, the targets.yaml block
    set_by: str = ""
    set_at: str = ""
    rejected: str = ""   # why the stored block is not in force; "" = it is, or no file

    @property
    def source(self) -> Literal["console", "targets.yaml"]:
        return "console" if self.revision and not self.rejected else "targets.yaml"

    def reject(self, reason: str) -> Origin:
        return replace(self, rejected=f"console model settings not applied: {reason}")


def read(state_dir: str | Path) -> tuple[object | None, Origin]:
    """The stored raw `models:` block (models.parse_block validates it) and
    its origin; None and the targets.yaml origin when there is no file. A
    file that is unreadable or not a {"models", ...} object is rejected, not
    raised: it must not stop a pass, nor the console page that can fix it."""
    path = Path(state_dir) / FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, Origin()
    except OSError as e:
        return None, Origin(revision=UNREADABLE).reject(f"{path} is unreadable: {e}")
    origin = Origin(revision=hashlib.sha256(data).hexdigest())
    try:
        doc = json.loads(data)
    except ValueError as e:
        return None, origin.reject(f"{path} is not JSON: {e}")
    if not isinstance(doc, dict) or "models" not in doc:
        return None, origin.reject(f"{path} holds no models block")
    return doc["models"], replace(origin, set_by=str(doc.get("set_by") or ""),
                                  set_at=str(doc.get("set_at") or ""))


def save(state_dir: str | Path, models: dict, *, actor: str, now: datetime) -> None:
    write_json_atomic(Path(state_dir) / FILE, {
        "models": models, "set_by": actor,
        "set_at": now.astimezone(timezone.utc).isoformat()})


def clear(state_dir: str | Path) -> None:
    (Path(state_dir) / FILE).unlink(missing_ok=True)
