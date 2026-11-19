"""Exam state on the box: `<state_dir>/exams/<owner>/<name>/<pr>/state.json`.

Every write holds the exams lock and renames atomically."""
from __future__ import annotations

import json
from pathlib import Path

from dispatcher.state import state_lock, write_json_atomic

LOCK = "exams.lock"


def _path(state_dir: str | Path, repo: str, pr: int) -> Path:
    return Path(state_dir) / "exams" / repo / str(pr) / "state.json"


def read(state_dir: str | Path, repo: str, pr: int) -> dict | None:
    try:
        return json.loads(_path(state_dir, repo, pr).read_text())
    except FileNotFoundError:
        return None


def request(state_dir: str | Path, repo: str, pr: int) -> dict:
    """Record that this PR's exam is wanted; a repeat finds the same state."""
    with state_lock(state_dir, LOCK):
        existing = read(state_dir, repo, pr)
        if existing is not None:
            return existing
        doc = {"repo": repo, "pr": pr, "state": "new", "head_sha": None, "debt": False,
               "detail": "", "attempt": 0, "wanted": True, "job": None, "status": None,
               "merged": False, "passed_concepts": []}
        write_json_atomic(_path(state_dir, repo, pr), doc, indent=2)
        return doc
