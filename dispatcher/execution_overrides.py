"""Durable one-shot model choices for the next execution of any work item."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


DIR = "execution-overrides"


@dataclass(frozen=True)
class ExecutionOverride:
    model: str = ""
    bypass_usage: bool = False


def _path(state_dir: str | Path, target: str, issue: int) -> Path:
    return Path(state_dir) / DIR / f"{target}-{issue}.json"


def save(state_dir: str | Path, target: str, issue: int,
         override: ExecutionOverride) -> None:
    path = _path(state_dir, target, issue)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(override), indent=2))
    tmp.replace(path)


def load(state_dir: str | Path, target: str,
         issue: int) -> ExecutionOverride | None:
    path = _path(state_dir, target, issue)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return ExecutionOverride(
            model=str(raw.get("model") or ""),
            bypass_usage=raw.get("bypass_usage") is True,
        )
    except (OSError, ValueError, AttributeError):
        return None


def delete(state_dir: str | Path, target: str, issue: int) -> None:
    _path(state_dir, target, issue).unlink(missing_ok=True)
