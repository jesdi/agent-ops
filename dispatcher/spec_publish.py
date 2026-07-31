"""Deterministic backstop for the spec-review gate: make sure a finished
spec draft is committed to the task branch and pushed to origin BEFORE the
human is pinged to review it, and build the GitHub URL the notifications
link to. The prompt (prompts/spec.md) asks the session to do all of this
itself; this module is what makes it a guarantee instead of an instruction
(spec: docs/specs/2026-07-31-spec-visibility-design.md).

Never raises: every git problem becomes PublishResult.error, because a
push failure must not block the review gate — review in the attached
session still works with a local-only spec."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT = 60  # per git command


@dataclass(frozen=True)
class PublishResult:
    url: str = ""    # set on success
    error: str = ""  # set on failure — exactly one of the two is non-empty


def spec_url(repo: str, branch: str, artifact: str) -> str:
    return f"https://github.com/{repo}/blob/{branch}/{artifact}"


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


def ensure_published(*, worktree: str, branch: str, repo: str, issue: int,
                     artifact: str, dry_run: bool = False) -> PublishResult:
    rel = relative_artifact(worktree, artifact)
    if rel is None:
        return PublishResult(error=f"unusable spec artifact path: {artifact!r}")
    if dry_run:
        print(f"[dry-run] ensure spec {rel} committed+pushed on {branch}")
        return PublishResult(url=spec_url(repo, branch, rel))
    try:
        status = _git(worktree, "status", "--porcelain", "--", rel)
        if status.returncode != 0:
            return PublishResult(
                error=f"git status failed: {status.stderr.strip()}")
        if status.stdout.strip():
            # Uncommitted (or untracked) draft — the agent forgot. Commit
            # ONLY the artifact path; anything else dirty in the worktree
            # is scratch the dispatcher has no business publishing.
            add = _git(worktree, "add", "--", rel)
            if add.returncode != 0:
                return PublishResult(
                    error=f"git add failed: {add.stderr.strip()}")
            commit = _git(worktree, "commit",
                          "-m", f"docs: draft spec for #{issue}", "--", rel)
            if commit.returncode != 0:
                return PublishResult(
                    error=f"git commit failed: {commit.stderr.strip()}")
        head = _git(worktree, "rev-parse", "HEAD")
        if head.returncode != 0:
            return PublishResult(
                error=f"git rev-parse failed: {head.stderr.strip()}")
        remote = _git(worktree, "ls-remote", "origin", f"refs/heads/{branch}")
        remote_sha = remote.stdout.split()[:1] if remote.returncode == 0 else []
        if remote_sha != [head.stdout.strip()]:
            push = _git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
            if push.returncode != 0:
                return PublishResult(
                    error=f"git push failed: {push.stderr.strip()}")
    except (OSError, subprocess.SubprocessError) as exc:
        return PublishResult(error=f"git invocation failed: {exc}")
    return PublishResult(url=spec_url(repo, branch, rel))
