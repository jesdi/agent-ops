"""Task-owned review artifacts: collect, publish, retain, and expire.

Sessions register files in .agent/artifacts.json. Only the dispatcher writes
this store; the web layer reads it. Content is copied before workspace teardown.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import spec_publish
from dispatcher.state import TERMINAL_STAGES, TaskState, _read

log = logging.getLogger(__name__)
TERMINAL = TERMINAL_STAGES
RETENTION_DAYS = 30
MAX_BYTES = 20 * 1024 * 1024
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")


def task_root(state_dir, target: str, issue: int) -> Path:
    key = hashlib.sha256(f"{target}\0{issue}".encode()).hexdigest()
    return Path(state_dir) / "artifacts" / key


def _write(path: Path, value: dict) -> None:
    text = json.dumps(value, indent=2, sort_keys=True)
    if path.exists() and path.read_text() == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def read(state_dir, target: str, issue: int) -> dict:
    path = task_root(state_dir, target, issue) / "index.json"
    if not path.exists():
        return {"items": [], "expires_at": "", "expired": False}
    return json.loads(path.read_text())


def archived_task(state_dir, target: str, issue: int) -> TaskState | None:
    task = _read(task_root(state_dir, target, issue) / "task.json")
    return task if task and task.stage in TERMINAL else None


def _source(worktree: str, raw: str) -> Path:
    root = Path(worktree).resolve()
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("artifact must be a file inside the task worktree")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("artifact exceeds 20 MiB")
    return path


def _registrations(task: TaskState) -> list[dict]:
    manifest = Path(task.worktree) / ".agent/artifacts.json"
    entries = []
    if manifest.exists():
        try:
            raw = json.loads(_source(task.worktree, str(manifest)).read_text())
            if not isinstance(raw, list) or len(raw) > 100:
                raise ValueError("expected an array of at most 100 artifacts")
            for entry in raw:
                if (not isinstance(entry, dict)
                        or not isinstance(entry.get("id"), str)
                        or not ID.fullmatch(entry["id"])
                        or not isinstance(entry.get("name"), str)
                        or not entry["name"].strip()
                        or len(entry["name"]) > 200
                        or not isinstance(entry.get("path"), str)):
                    raise ValueError("invalid artifact registration")
            if len({e["id"] for e in raw}) != len(raw):
                raise ValueError("duplicate artifact IDs")
            entries = raw
        except (OSError, ValueError) as exc:
            log.warning("Invalid artifact manifest for %s/%s: %s", task.target, task.issue, exc)
    # Existing sessions do not yet know the manifest protocol. Preserve the
    # durable spec and current question artifact automatically.
    if task.spec_path:
        entries = [e for e in entries if e["id"] != "spec" and e["path"] != task.spec_path]
        entries.insert(0, {"id": "spec", "name": "Specification", "path": task.spec_path})
    # Backfill known review outputs from sessions started before registration
    # was introduced; do not sweep arbitrary worktree files.
    for artifact_id, name, path in (
        ("prototype", "Prototype", ".agent/prototype.html"),
        ("questionnaire", "Questionnaire", ".agent/questionnaire.md"),
    ):
        if ((Path(task.worktree) / path).is_file()
                and not any(e["id"] == artifact_id or e["path"] == path for e in entries)):
            entries.append({"id": artifact_id, "name": name, "path": path})
    request_path = getattr(task.operator_request, "path", "")
    if request_path and not any(e["path"] == request_path for e in entries):
        key = hashlib.sha256(request_path.encode()).hexdigest()[:12]
        entries.append({"id": f"answers-{key}", "name": "Questions and answers", "path": request_path})
    return entries


def collect(state_dir, task: TaskState, repo: str, *, publish: bool = True,
            now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    root = task_root(state_dir, task.target, task.issue)
    index = read(state_dir, task.target, task.issue)
    terminal_at = task.terminal_at or (task.done_at or task.updated_at if task.stage in TERMINAL else "")
    expires = ""
    if task.stage in TERMINAL and terminal_at:
        since = datetime.fromisoformat(terminal_at)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        expires = (since + timedelta(days=RETENTION_DAYS)).isoformat()
    # Reopening cancels the countdown and permits content collection again.
    if not expires:
        index["expired"] = False
    index.update(target=task.target, issue=task.issue, expires_at=expires)
    items = {e["id"]: e for e in index["items"]}
    if not index["expired"] and Path(task.worktree).is_dir():
        for entry in _registrations(task):
            try:
                path = _source(task.worktree, entry["path"])
                rel = path.relative_to(Path(task.worktree).resolve()).as_posix()
                content = path.read_bytes()
                if len(content) > MAX_BYTES:
                    raise ValueError("artifact exceeds 20 MiB")
            except (OSError, ValueError) as exc:
                log.warning("Cannot collect artifact %s/%s/%s: %s", task.target, task.issue, entry["id"], exc)
                continue
            digest = hashlib.sha256(content).hexdigest()
            old = items.get(entry["id"], {})
            changed = old.get("digest") != digest or old.get("path") != rel
            markdown = path.suffix.lower() in {".md", ".markdown"}
            item = dict(old, id=entry["id"], name=entry["name"], path=rel,
                        media_type="text/markdown" if markdown else (mimetypes.guess_type(rel)[0] or "application/octet-stream"),
                        digest=digest, stage=old.get("stage", task.stage.value),
                        updated_at=now.isoformat() if changed else old["updated_at"])
            if changed:
                item.update(github_url="", published_commit="")
            dest = root / "content" / entry["id"]
            if changed or not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(content)
                tmp.replace(dest)
            # Retry unpublished Markdown each pass; never expose a guessed link.
            if markdown and publish and repo and not item.get("github_url"):
                url, commit = spec_publish.published_reference(
                    worktree=task.worktree, branch=task.branch, repo=repo,
                    artifact=rel, content=content)
                item.update(github_url=url, published_commit=commit)
            items[item["id"]] = item
    index["items"] = list(items.values())
    _write(root / "index.json", index)
    if task.stage in TERMINAL:
        snapshot = asdict(task)
        snapshot["stage"] = task.stage.value
        _write(root / "task.json", snapshot)


def pin_published(state_dir, task: TaskState, repo: str) -> None:
    """Before deleting a merged task branch, keep GitHub destinations alive."""
    index = read(state_dir, task.target, task.issue)
    for item in index["items"]:
        if item.get("github_url") and item.get("published_commit"):
            item["github_url"] = spec_publish.spec_url(repo, item["published_commit"], item["path"])
    _write(task_root(state_dir, task.target, task.issue) / "index.json", index)


def cleanup(state_dir, *, now: datetime | None = None) -> None:
    """Delete only stored content; retain metadata, GitHub URLs and task context."""
    now = now or datetime.now(timezone.utc)
    for index_path in (Path(state_dir) / "artifacts").glob("*/index.json"):
        try:
            index = json.loads(index_path.read_text())
            if not index.get("expires_at") or index.get("expired"):
                continue
            if now < datetime.fromisoformat(index["expires_at"]):
                continue
            content = index_path.parent / "content"
            if content.exists():
                for path in content.iterdir():
                    if path.is_file() or path.is_symlink():
                        path.unlink()
            index["expired"] = True
            _write(index_path, index)
        except (OSError, ValueError):
            log.exception("Artifact cleanup failed for %s", index_path)


def content_path(state_dir, target: str, issue: int, artifact_id: str) -> Path | None:
    if not ID.fullmatch(artifact_id):
        return None
    root = task_root(state_dir, target, issue)
    path = root / "content" / artifact_id
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path
