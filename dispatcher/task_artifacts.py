"""Task-owned review artifacts: collect, publish, retain, and expire.

Sessions register files in .agent/artifacts.json. Only the dispatcher writes
this store; the web layer reads it. Content is copied before workspace teardown.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import answered_page, answers, host_auth, spec_publish, state
from dispatcher.artifacts import REVIEW_PAGE_MAX_BYTES
from dispatcher.state import TaskState, operator_request

log = logging.getLogger(__name__)
RETENTION_DAYS = 7
MAX_BYTES = 20 * 1024 * 1024
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
# The artifact each answers kind is captured as.
ANSWERS_ARTIFACTS = {"plan-approval": ("review-answers", "Review answers"),
                     "answers": ("questionnaire-answers", "Questionnaire answers")}
# The page a text answer of each kind is captured on when no request of its kind is open.
DEFAULT_PAGES = {"plan-approval": ".agent/review.html", "answers": ".agent/questionnaire.html"}


@dataclass(frozen=True)
class Registration:
    id: str
    name: str
    path: str

    @classmethod
    def parse(cls, raw: dict) -> Registration:
        entry = cls(**{key: raw[key] for key in ("id", "name", "path")})
        if not all(isinstance(value, str) for value in asdict(entry).values()):
            raise ValueError("artifact fields must be strings")
        if not ID.fullmatch(entry.id):
            raise ValueError("invalid artifact ID")
        if not entry.name.strip() or len(entry.name) > 200:
            raise ValueError("invalid artifact name")
        return entry


@dataclass
class StoredArtifact:
    id: str
    name: str
    path: str
    media_type: str
    digest: str
    stage: str
    updated_at: str
    publication: spec_publish.PublishedReference | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> StoredArtifact:
        values = dict(raw)
        url = values.pop("github_url", "")
        commit = values.pop("published_commit", "")
        publication = spec_publish.PublishedReference(url, commit) if url else None
        return cls(**values, publication=publication)

    def to_dict(self) -> dict:
        values = asdict(self)
        values.pop("publication")
        values["github_url"] = self.publication.url if self.publication else ""
        values["published_commit"] = self.publication.commit if self.publication else ""
        return values


@dataclass
class ArtifactIndex:
    items: list[StoredArtifact] = field(default_factory=list)
    expires_at: str = ""
    expired: bool = False
    repository: str = ""
    issue: int = 0

    @classmethod
    def load(cls, path: Path) -> ArtifactIndex:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        if "target" in raw:   # files written before the rename
            raw["repository"] = raw.pop("target")
        raw["items"] = [StoredArtifact.from_dict(item) for item in raw["items"]]
        return cls(**raw)

    def save(self, path: Path) -> None:
        value = asdict(self)
        value["items"] = [item.to_dict() for item in self.items]
        _write(path, value)

    def retain_for(self, task: TaskState) -> None:
        self.repository, self.issue = task.repository, task.issue
        self.expires_at = ""
        if task.terminal_at:
            since = datetime.fromisoformat(task.terminal_at)
            since = since.replace(tzinfo=since.tzinfo or timezone.utc)
            self.expires_at = (since + timedelta(days=RETENTION_DAYS)).isoformat()
        else:
            self.expired = False

    def expire(self, path: Path, now: datetime) -> None:
        if self.expired or not self.expires_at:
            return
        if now < datetime.fromisoformat(self.expires_at):
            return
        for content in (path.parent / "content").glob("*"):
            if content.is_file() or content.is_symlink():
                content.unlink()
        self.expired = True
        self.save(path)


def _write(path: Path, value: dict) -> None:
    text = json.dumps(value, indent=2, sort_keys=True)
    if path.exists() and path.read_text() == text:
        return
    state.write_atomic(path, text)


def read(state_dir, repository: str, issue: int) -> ArtifactIndex:
    return ArtifactIndex.load(state.archive_root(state_dir, repository, issue) / "index.json")


def _source(worktree: str, raw: str) -> Path:
    root = Path(worktree).resolve()
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("artifact must be a file inside the task worktree")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("artifact exceeds 20 MiB")
    return path


def _manifest(task: TaskState) -> list[Registration]:
    manifest = Path(task.worktree) / ".agent/artifacts.json"
    if not manifest.exists():
        return []
    try:
        entries = _parse_manifest(json.loads(_source(task.worktree, str(manifest)).read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Invalid artifact manifest for %s/%s: %s", task.repository, task.issue, exc)
        return []
    # The answers ids belong to the capture, never to a session's copy.
    reserved = {artifact_id for artifact_id, _ in ANSWERS_ARTIFACTS.values()}
    return [entry for entry in entries if entry.id not in reserved]


def _parse_manifest(raw: object) -> list[Registration]:
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("expected an array of at most 100 artifacts")
    entries = [Registration.parse(entry) for entry in raw]
    if len({entry.id for entry in entries}) != len(entries):
        raise ValueError("duplicate artifact IDs")
    return entries


def _automatic_registrations(task: TaskState) -> list[Registration]:
    entries = []
    for artifact_id, name, path in (
        ("prototype", "Prototype", ".agent/prototype.html"),
    ):
        if (Path(task.worktree) / path).is_file():
            entries.append(Registration(artifact_id, name, path))
    request_path = getattr(operator_request(task), "path", "")
    if request_path:
        key = hashlib.sha256(request_path.encode()).hexdigest()[:12]
        entries.append(Registration(f"answers-{key}", "Questions and answers", request_path))
    return entries


def _spec_registrations(task: TaskState) -> list[Registration]:
    entries = _manifest(task)
    if task.spec_path:
        entries = [entry for entry in entries if entry.id != "spec" and entry.path != task.spec_path]
        entries.insert(0, Registration("spec", "Specification", task.spec_path))
    return entries


def _registrations(task: TaskState) -> list[Registration]:
    entries = _spec_registrations(task)
    ids = {entry.id for entry in entries}
    paths = {entry.path for entry in entries}
    for entry in _automatic_registrations(task):
        if entry.id not in ids and entry.path not in paths:
            entries.append(entry)
            ids.add(entry.id)
            paths.add(entry.path)
    return entries


class Collector:
    """One collection pass owns content snapshots and a single remote lookup."""

    def __init__(self, root: Path, task: TaskState, repo: str, publish: bool, now: datetime,
                 auth: host_auth.HostAuth = host_auth.STORED):
        self.root, self.task, self.now = root, task, now
        self.publisher = (spec_publish.ArtifactPublisher(task.worktree, task.branch, repo, auth)
                          if publish and repo else None)

    def store(self, entry: Registration, old: StoredArtifact | None) -> StoredArtifact:
        path = _source(self.task.worktree, entry.path)
        rel = path.relative_to(Path(self.task.worktree).resolve()).as_posix()
        content = path.read_bytes()
        if len(content) > MAX_BYTES:
            raise ValueError("artifact exceeds 20 MiB")
        digest = hashlib.sha256(content).hexdigest()
        changed = old is None or (old.digest, old.path) != (digest, rel)
        if changed:
            item = StoredArtifact(entry.id, entry.name, rel, _media_type(path), digest,
                                  old.stage if old else self.task.stage.value, self.now.isoformat())
        else:
            item = replace(old, name=entry.name)
        dest = self.root / "content" / entry.id
        if changed or not dest.exists():
            _copy_content(dest, content)
        self.publish(item, content)
        return item

    def publish(self, item: StoredArtifact, content: bytes) -> None:
        if self.publisher and item.media_type == "text/markdown" and not item.publication:
            item.publication = self.publisher.reference(item.path, content)

    def collect(self, index: ArtifactIndex) -> None:
        items = {item.id: item for item in index.items}
        for entry in _registrations(self.task):
            try:
                items[entry.id] = self.store(entry, items.get(entry.id))
            except (OSError, ValueError) as exc:
                log.warning("Cannot collect artifact %s/%s/%s: %s", self.task.repository, self.task.issue, entry.id, exc)
        index.items = list(items.values())
        self.capture(index)

    def capture(self, index: ArtifactIndex) -> None:
        for kind, (artifact_id, _) in ANSWERS_ARTIFACTS.items():
            try:
                _capture(self.root, index, self.task, kind, self.now)
            except (OSError, ValueError) as exc:
                log.warning("Cannot capture artifact %s/%s/%s: %s", self.task.repository, self.task.issue, artifact_id, exc)


def _copy_content(dest: Path, content: bytes) -> None:
    state.write_atomic(dest, content)


def _media_type(path: Path) -> str:
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def collect(state_dir, task: TaskState, repo: str, *, publish: bool = True,
            now: datetime | None = None,
            auth: host_auth.HostAuth = host_auth.STORED) -> None:
    """`auth`: the pass's host credentials, which the remote lookup runs on."""
    now = now or datetime.now(timezone.utc)
    root = state.archive_root(state_dir, task.repository, task.issue)
    index = read(state_dir, task.repository, task.issue)
    index.retain_for(task)
    if not index.expired and Path(task.worktree).is_dir():
        Collector(root, task, repo, publish, now, auth).collect(index)
    index.save(root / "index.json")
    state.archive(state_dir, task)


def _capture(root: Path, index: ArtifactIndex, task: TaskState, kind: str, now: datetime) -> None:
    """Archive the answered page of the task's answers file of `kind` when it
    is a submission the index does not hold yet. The item's digest is the
    answers file's, not the page's. The page is written in atomically and only
    then entered in `index`, so a failed write leaves the earlier page and its
    entry. The caller saves `index`. Raises OSError or ValueError when the
    file or the page cannot be used."""
    name, _ = answers.ANSWERS_FILES[kind]
    path = f".agent/{name}"
    submission = _submission(task, path)
    if submission is None:
        return
    raw, doc = submission
    artifact_id, artifact_name = ANSWERS_ARTIFACTS[kind]
    digest = hashlib.sha256(raw).hexdigest()
    items = {item.id: item for item in index.items}
    if artifact_id in items and items[artifact_id].digest == digest:
        return
    page = answered_page.render(_request_page(task, kind), doc.get("answers")).encode()
    _copy_content(root / "content" / artifact_id, page)
    items[artifact_id] = StoredArtifact(artifact_id, artifact_name, path, "text/html", digest,
                                        task.stage.value, now.isoformat())
    index.items = list(items.values())


def _submission(task: TaskState, path: str) -> tuple[bytes, dict] | None:
    """The answers file at `path` and its document, when it is a submission."""
    raw = state.read_regular(Path(task.worktree) / path, answers.ANSWERS_FILE_MAX_BYTES)
    doc = json.loads(raw) if raw is not None else None
    if not isinstance(doc, dict) or doc.get("submitted") is None:
        return None
    return raw, doc


def _request_page(task: TaskState, kind: str) -> str:
    """The open request's page when it is of `kind`, else the default page of `kind`."""
    request = operator_request(task)
    page_path = request.path if getattr(request, "kind", None) == kind else DEFAULT_PAGES[kind]
    page = state.read_regular(Path(task.worktree) / page_path, REVIEW_PAGE_MAX_BYTES)
    if page is None:
        raise OSError("request page is not a readable regular file")
    return page.decode()


def capture_answers(state_dir, task: TaskState, now: datetime | None = None) -> None:
    """Archive the answered page of the submission just recorded for the
    task's open request. When the index cannot be saved, the earlier page is
    put back, so the served page and its entry never disagree."""
    kind = operator_request(task).kind
    root = state.archive_root(state_dir, task.repository, task.issue)
    index = read(state_dir, task.repository, task.issue)
    index.retain_for(task)
    content = root / "content" / ANSWERS_ARTIFACTS[kind][0]
    earlier = content.with_name(f".{content.name}.earlier")
    earlier.unlink(missing_ok=True)
    if content.is_file():
        os.link(content, earlier)   # a rename back needs no space on a full disk
    try:
        _capture(root, index, task, kind, now or datetime.now(timezone.utc))
        try:
            index.save(root / "index.json")
        except BaseException:
            if earlier.exists():
                os.replace(earlier, content)
            else:
                content.unlink(missing_ok=True)
            raise
    finally:
        earlier.unlink(missing_ok=True)


def pin_published(state_dir, task: TaskState, repo: str) -> None:
    """Before deleting a merged task branch, keep GitHub destinations alive."""
    index = read(state_dir, task.repository, task.issue)
    for item in index.items:
        if item.publication and item.publication.commit:
            item.publication = spec_publish.PublishedReference(
                spec_publish.spec_url(repo, item.publication.commit, item.path), item.publication.commit)
    index.save(state.archive_root(state_dir, task.repository, task.issue) / "index.json")


def cleanup(state_dir, *, now: datetime | None = None) -> None:
    """Delete stored content; retain metadata, GitHub URLs and task context."""
    now = now or datetime.now(timezone.utc)
    for path in (Path(state_dir) / "artifacts").glob("*/index.json"):
        try:
            ArtifactIndex.load(path).expire(path, now)
        except (OSError, ValueError):
            log.exception("Artifact cleanup failed for %s", path)


def content_path(state_dir, repository: str, issue: int, artifact_id: str) -> Path | None:
    if not ID.fullmatch(artifact_id):
        return None
    root = state.archive_root(state_dir, repository, issue)
    path = root / "content" / artifact_id
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path
