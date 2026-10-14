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
from typing import Literal

from dispatcher.state import write_json_atomic

FILE = "model-settings.json"
UNREADABLE = "unreadable"   # the revision of a file that cannot be read


@dataclass(frozen=True)
class Found:
    """A stored block read from the file, not yet checked (read())."""
    set_by: str
    set_at: str
    block: object   # the raw `models:` value: models.parse_block checks it


@dataclass(frozen=True)
class Stored:
    """The console's block is in force (config.apply_stored_models)."""
    set_by: str
    set_at: str


@dataclass(frozen=True)
class Rejected:
    """A stored block that is not in force, and why: targets.yaml's runs."""
    reason: str


@dataclass(frozen=True)
class Origin:
    """Where the block in force comes from. `revision` is the digest of the
    file it was read from ("" = no file); a save or reset names it, so one
    from a page that read an older file is refused."""
    revision: str = ""
    state: Stored | Rejected | None = None   # None: no file, targets.yaml's block

    @property
    def source(self) -> Literal["console", "targets.yaml"]:
        return "console" if isinstance(self.state, Stored) else "targets.yaml"

    @property
    def set_by(self) -> str:
        return self.state.set_by if isinstance(self.state, Stored) else ""

    @property
    def set_at(self) -> str:
        return self.state.set_at if isinstance(self.state, Stored) else ""

    @property
    def error(self) -> str:
        if not isinstance(self.state, Rejected):
            return ""
        return f"console model settings not applied: {self.state.reason}"


def read(state_dir: str | Path) -> tuple[str, Found | Rejected | None]:
    """The file's revision ("" = no file) and what it holds. A file that
    cannot be read or is not a {"models", ...} object is Rejected, not
    raised: it must not stop a pass, nor the console page that can fix it."""
    path = Path(state_dir) / FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "", None
    except OSError as e:
        return UNREADABLE, Rejected(f"{path} is unreadable: {e}")
    revision = hashlib.sha256(data).hexdigest()
    try:
        doc = json.loads(data)
    except ValueError as e:
        return revision, Rejected(f"{path} is not JSON: {e}")
    if not isinstance(doc, dict) or "models" not in doc:
        return revision, Rejected(f"{path} holds no models block")
    return revision, Found(str(doc.get("set_by") or ""), str(doc.get("set_at") or ""),
                           doc["models"])


def save(state_dir: str | Path, models: dict, *, actor: str, now: datetime) -> None:
    write_json_atomic(Path(state_dir) / FILE, {
        "models": models, "set_by": actor,
        "set_at": now.astimezone(timezone.utc).isoformat()})


def clear(state_dir: str | Path) -> None:
    (Path(state_dir) / FILE).unlink(missing_ok=True)
