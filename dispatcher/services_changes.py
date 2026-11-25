"""The unrecorded-changes file `<state_dir>/services-changes.yaml`: console edits to a
managed job that the deploy sync applies over the installed config until they are recorded."""
from __future__ import annotations

from pathlib import Path

import yaml

from dispatcher.state import state_lock


def set_paused(state_dir: str, job: str, paused: bool, apply) -> None:
    """Run `apply()` (the systemctl call), then record `paused` for `job`, all under
    one lock. An entry equal to the installed config is dropped; if `apply` raises,
    nothing is written."""
    root = Path(state_dir)
    with state_lock(state_dir, "services-changes.lock", None):
        apply()
        installed = (root / "services.yaml")
        base = (yaml.safe_load(installed.read_text()) or {}) if installed.exists() else {}
        file = root / "services-changes.yaml"
        changes = (yaml.safe_load(file.read_text()) or {}) if file.exists() else {}
        entry = changes.get(job) or {}
        if paused == bool((base.get(job) or {}).get("paused", False)):
            entry.pop("paused", None)
        else:
            entry["paused"] = paused
        if entry:
            changes[job] = entry
        else:
            changes.pop(job, None)
        tmp = file.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(changes))
        tmp.replace(file)
