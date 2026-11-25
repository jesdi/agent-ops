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


def failed(entry: dict) -> bool:
    """Whether the recorder marked the entry's recording failed."""
    return bool((entry.get("record") or {}).get("failed"))


def _kept_record(entry: dict) -> dict:
    """The entry's record without its failed mark: a new change clears it."""
    return {k: v for k, v in (entry.get("record") or {}).items() if k != "failed"}


def _with_record(entry: dict, rec: dict) -> dict:
    return {**entry, "record": rec} if rec else entry


def set_paused(state_dir: str, job: str, paused: bool, apply) -> None:
    """Run `apply()` (the systemctl call), then record `paused` for `job`, all under
    one lock. An entry equal to the installed config is dropped, unless the recorder
    has pushed or recorded for it (it must still close, supersede or undo that); if `apply` raises,
    nothing is written. A new change clears a failed recording."""
    root = Path(state_dir)
    with state_lock(state_dir, "services-changes.lock", None):
        apply()
        same = paused == bool((_read(root / "services.yaml").get(job) or {}).get("paused", False))

        def edit(entry: dict) -> dict:
            rec = _kept_record(entry)
            keep = {k: v for k, v in entry.items() if k == "schedule"}
            if same and not rec:
                return keep
            return _with_record({**keep, "paused": paused}, rec)
        _update(root, job, edit)


class Applying(Exception):
    """A schedule change is already waiting to be applied."""


def set_schedule(state_dir: str, job: str, schedule: dict) -> None:
    """Record `schedule` for `job`, under the changes lock. Raises `Applying` while an earlier
    change is not yet installed (a second save is refused, never merged); a failed one is replaced."""
    with state_lock(state_dir, "services-changes.lock", None):
        def edit(entry: dict) -> dict:
            if entry and not failed(entry):
                raise Applying()
            return _with_record({**{k: v for k, v in entry.items() if k != "record"}, "schedule": schedule},
                                _kept_record(entry))
        _update(Path(state_dir), job, edit)
