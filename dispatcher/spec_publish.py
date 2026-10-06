"""Deterministic backstop for the plan-review gate: make sure the task's
spec folder is committed to the task branch and pushed to origin BEFORE the
human is pinged to review it, and build the GitHub URL the notifications
link to. The prompts (prompts/spec.md, prompts/plan.md) ask the sessions to
do all of this themselves; this module is what makes it a guarantee instead
of an instruction (spec: docs/specs/2026-07-31-spec-visibility-design.md).

Never raises: every git problem becomes PublishResult.error, because a
push failure must not block the review gate — review in the attached
session still works with a local-only spec."""
from __future__ import annotations

import subprocess
from urllib.parse import quote
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT = 60  # per git command


@dataclass(frozen=True)
class PublishResult:
    url: str = ""    # set on success
    error: str = ""  # set on failure — exactly one of the two is non-empty


def spec_url(repo: str, branch: str, artifact: str) -> str:
    return f"https://github.com/{repo}/blob/{quote(branch, safe='/')}/{quote(artifact, safe='/')}"


def folder_url(repo: str, branch: str, folder: str) -> str:
    return f"https://github.com/{repo}/tree/{quote(branch, safe='/')}/{quote(folder, safe='/')}"


def relative_artifact(worktree: str, artifact: str) -> str | None:
    """Worktree-relative artifact path, or None when it's empty or points
    outside the worktree (stage.json is model-written — treat it as
    untrusted input, not a crash source)."""
    if not artifact:
        return None
    p = Path(artifact)
    if not p.is_absolute():
        return p.as_posix()
    for candidate in (p, Path(str(p)).resolve()):
        for root in (Path(worktree), Path(worktree).resolve()):
            try:
                return candidate.relative_to(root).as_posix()
            except ValueError:
                continue
    return None


def _git(worktree: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", worktree, *args],
                          capture_output=True, text=True, timeout=_TIMEOUT)


class _GitFailed(Exception):
    pass


def _must(worktree: str, *args: str) -> subprocess.CompletedProcess:
    proc = _git(worktree, *args)
    if proc.returncode != 0:
        raise _GitFailed(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc


def _commit_and_push(worktree: str, branch: str, issue: int, spec: str) -> None:
    rel = Path(spec).parent.as_posix()
    if _must(worktree, "status", "--porcelain", "--", rel).stdout.strip():
        # Uncommitted (or untracked) files — the agent forgot. Commit
        # ONLY the spec folder; anything else dirty in the worktree
        # is scratch the dispatcher has no business publishing.
        _must(worktree, "add", "--", rel)
        _must(worktree, "commit", "-m", f"docs: spec folder for #{issue}", "--", rel)
    # The spec itself, not just its folder: a deleted or ignored spec.md
    # beside a tracked design.md must not get a link.
    if _git(worktree, "ls-files", "--error-unmatch", "--", spec).returncode != 0:
        raise _GitFailed(f"spec not tracked in git: {spec}")
    head = _must(worktree, "rev-parse", "HEAD").stdout.strip()
    remote = _git(worktree, "ls-remote", "origin", f"refs/heads/{branch}")
    if remote.returncode != 0 or remote.stdout.split()[:1] != [head]:
        _must(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")


def ensure_published(*, worktree: str, branch: str, repo: str, issue: int,
                     artifact: str, dry_run: bool = False) -> PublishResult:
    """Publish the folder `artifact` (the task's spec.md) lives in."""
    spec = relative_artifact(worktree, artifact)
    parts = Path(spec).parts if spec else ()
    if len(parts) < 3 or ".." in parts or parts[0] == ".agent":
        # The folder is committed whole, so the spec needs one of its own
        # below a parent (specs/<slug>/spec.md): a shallower path would
        # publish the worktree or every spec, and `.agent/` is never pushed.
        return PublishResult(error=f"unusable spec artifact path: {artifact!r}")
    rel = Path(spec).parent.as_posix()
    if dry_run:
        print(f"[dry-run] ensure spec folder {rel} committed+pushed on {branch}")
        return PublishResult(url=folder_url(repo, branch, rel))
    try:
        _commit_and_push(worktree, branch, issue, spec)
    except _GitFailed as exc:
        return PublishResult(error=str(exc))
    except (OSError, subprocess.SubprocessError) as exc:
        return PublishResult(error=f"git invocation failed: {exc}")
    return PublishResult(url=folder_url(repo, branch, rel))


@dataclass(frozen=True)
class PublishedReference:
    url: str
    commit: str


class ArtifactPublisher:
    """Verify artifacts against one remote branch snapshot per collection."""

    def __init__(self, worktree: str, branch: str, repo: str):
        self.worktree, self.branch, self.repo = worktree, branch, repo
        self._resolved = False
        self._commit = ""

    def _remote_commit(self) -> str:
        if not self._resolved:
            self._resolved = True
            remote = _git(self.worktree, "ls-remote", "origin", f"refs/heads/{self.branch}")
            fields = remote.stdout.split() if remote.returncode == 0 else []
            self._commit = fields[0] if fields else ""
        return self._commit

    def reference(self, artifact: str, content: bytes) -> PublishedReference | None:
        rel = relative_artifact(self.worktree, artifact)
        if not rel:
            return None
        try:
            commit = self._remote_commit()
            if not commit:
                return None
            blob = _git(self.worktree, "rev-parse", f"{commit}:{rel}")
            hashed = subprocess.run(["git", "-C", self.worktree, "hash-object", "--stdin"],
                                    input=content, capture_output=True, timeout=_TIMEOUT)
            if (blob.returncode != 0 or hashed.returncode != 0
                    or blob.stdout.strip().encode() != hashed.stdout.strip()):
                return None
            return PublishedReference(spec_url(self.repo, self.branch, rel), commit)
        except (OSError, subprocess.SubprocessError):
            return None
