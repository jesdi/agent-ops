"""The unrecorded-changes file `<state_dir>/services-changes.yaml`: console edits to a
managed job that the deploy sync applies over the installed config until they are recorded."""
from __future__ import annotations

from pathlib import Path

import yaml

from dispatcher.state import state_lock, write_atomic


def _read(file: Path) -> dict:
    return (yaml.safe_load(file.read_text()) or {}) if file.exists() else {}


def read(state_dir: str) -> dict:
    return _read(Path(state_dir) / "services-changes.yaml")


def update(state_dir: str, job: str, edit) -> None:
    """Under the changes lock, replace `job`'s entry by `edit(entry)`; {} drops it."""
    with state_lock(state_dir, "services-changes.lock", None):
        _update(Path(state_dir), job, edit)


def _update(root: Path, job: str, edit) -> None:
    file = root / "services-changes.yaml"
    changes = _read(file)
    entry = edit(dict(changes.get(job) or {}))
    if entry:
        changes[job] = entry
    else:
        changes.pop(job, None)
    write_atomic(file, yaml.safe_dump(changes))


def set_paused(state_dir: str, job: str, paused: bool, apply) -> None:
    """Run `apply()` (the systemctl call), then record `paused` for `job`, all under
    one lock. An entry equal to the installed config is dropped, unless the recorder
    has pushed or recorded for it (it must still close, supersede or undo that); if `apply` raises,
    nothing is written. A new change clears a failed recording."""
    root = Path(state_dir)
    with state_lock(state_dir, "services-changes.lock", None):
        apply()
        installed = (root / "services.yaml")
        base = (yaml.safe_load(installed.read_text()) or {}) if installed.exists() else {}
        same = paused == bool((base.get(job) or {}).get("paused", False))

        def edit(entry: dict) -> dict:
            rec = {k: v for k, v in (entry.get("record") or {}).items() if k != "failed"}
            if same and not rec:
                return {}
            return {"paused": paused, **({"record": rec} if rec else {})}
        _update(root, job, edit)
