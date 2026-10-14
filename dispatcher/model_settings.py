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
class Stored:
    """The console's block is in force (or, from read(), is to be checked)."""
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
    def error(self) -> str:
        return self.state.reason if isinstance(self.state, Rejected) else ""

    def reject(self, reason: str) -> Origin:
        return Origin(self.revision, Rejected(f"console model settings not applied: {reason}"))


def read(state_dir: str | Path) -> tuple[Origin, object]:
    """The file's origin and its raw `models:` block. The block means
    something only when the state is Stored, and models.parse_block still
    validates it (it may be null, or anything). A file that cannot be read
    or is not a {"models", ...} object is Rejected, not raised: it must not
    stop a pass, nor the console page that can fix it."""
    path = Path(state_dir) / FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Origin(), None
    except OSError as e:
        return Origin(UNREADABLE).reject(f"{path} is unreadable: {e}"), None
    origin = Origin(hashlib.sha256(data).hexdigest())
    try:
        doc = json.loads(data)
    except ValueError as e:
        return origin.reject(f"{path} is not JSON: {e}"), None
    if not isinstance(doc, dict) or "models" not in doc:
        return origin.reject(f"{path} holds no models block"), None
    stored = Stored(str(doc.get("set_by") or ""), str(doc.get("set_at") or ""))
    return Origin(origin.revision, stored), doc["models"]


def save(state_dir: str | Path, models: dict, *, actor: str, now: datetime) -> None:
    write_json_atomic(Path(state_dir) / FILE, {
        "models": models, "set_by": actor,
        "set_at": now.astimezone(timezone.utc).isoformat()})


def clear(state_dir: str | Path) -> None:
    (Path(state_dir) / FILE).unlink(missing_ok=True)
