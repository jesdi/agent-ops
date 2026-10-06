"""ensure_published against real temp git repos: a bare origin plus a
worktree clone, the same shape the dispatcher provisions per task."""
import subprocess

import pytest

from dispatcher.spec_publish import (PublishResult, ensure_published,
                                     folder_url, relative_artifact, spec_url)

BRANCH = "agent/task-7"
REPO = "jesdi/portfolio_eval"
FOLDER = "specs/2026-07-31-widget"
SPEC_REL = f"{FOLDER}/spec.md"
FOLDER_URL = f"https://github.com/{REPO}/tree/{BRANCH}/{FOLDER}"


def _git(cwd, *args) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True)
    return proc.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    path = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def wt(tmp_path, origin):
    path = tmp_path / "wt"
    subprocess.run(["git", "init", "-b", BRANCH, str(path)],
                   check=True, capture_output=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("seed\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    _git(path, "remote", "add", "origin", str(origin))
    (path / SPEC_REL).parent.mkdir(parents=True)
    return path


def _publish(wt, artifact=SPEC_REL, dry_run=False):
    return ensure_published(worktree=str(wt), branch=BRANCH, repo=REPO,
                            issue=7, artifact=artifact, dry_run=dry_run)


def test_spec_url_shape():
    assert (spec_url(REPO, BRANCH, SPEC_REL)
            == f"https://github.com/{REPO}/blob/{BRANCH}/{SPEC_REL}")


def test_folder_url_shape():
    assert folder_url(REPO, BRANCH, FOLDER) == FOLDER_URL


def test_relative_artifact_passthrough_and_absolute(tmp_path):
    assert relative_artifact(str(tmp_path), SPEC_REL) == SPEC_REL
    assert relative_artifact(str(tmp_path),
                             str(tmp_path / SPEC_REL)) == SPEC_REL
    assert relative_artifact(str(tmp_path), "") is None
    assert relative_artifact(str(tmp_path), "/somewhere/else.md") is None


def test_uncommitted_spec_gets_committed_and_pushed(wt, origin):
    (wt / SPEC_REL).write_text("# design\n")
    res = _publish(wt)
    assert res.error == ""
    assert res.url == FOLDER_URL
    # committed with the backstop message, only the spec folder staged
    assert _git(wt, "log", "-1", "--pretty=%s") == "docs: spec folder for #7"
    assert _git(wt, "status", "--porcelain", "--", SPEC_REL) == ""
    # pushed: origin tip == local tip
    assert (_git(origin, "rev-parse", f"refs/heads/{BRANCH}")
            == _git(wt, "rev-parse", "HEAD"))


def test_the_whole_folder_is_published_when_any_file_is_uncommitted(wt, origin):
    (wt / SPEC_REL).write_text("# spec\n")
    (wt / FOLDER / "proposal.md").write_text("# proposal\n")
    _git(wt, "add", "."); _git(wt, "commit", "-m", "stage 1")
    (wt / SPEC_REL).write_text("# spec, corrected\n")
    (wt / FOLDER / "design.md").write_text("# design\n")
    assert _publish(wt).url == FOLDER_URL
    shown = _git(origin, "ls-tree", "-r", "--name-only", BRANCH)
    assert {f"{FOLDER}/{n}" for n in ("proposal.md", "spec.md", "design.md")} <= set(shown.split())
    assert _git(origin, "show", f"{BRANCH}:{SPEC_REL}") == "# spec, corrected"


def test_other_dirty_files_are_not_swept_into_the_commit(wt):
    (wt / SPEC_REL).write_text("# design\n")
    (wt / "unrelated.txt").write_text("scratch\n")
    res = _publish(wt)
    assert res.error == ""
    assert "unrelated.txt" in _git(wt, "status", "--porcelain")
    assert "unrelated" not in _git(wt, "show", "--stat", "HEAD")


def test_committed_but_unpushed_gets_pushed_without_new_commit(wt, origin):
    (wt / SPEC_REL).write_text("# design\n")
    _git(wt, "add", "."); _git(wt, "commit", "-m", "docs: draft spec for #7")
    before = _git(wt, "rev-parse", "HEAD")
    res = _publish(wt)
    assert res.error == ""
    assert _git(wt, "rev-parse", "HEAD") == before  # no second commit
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}") == before


def test_already_pushed_is_a_noop(wt, origin):
    (wt / SPEC_REL).write_text("# design\n")
    _git(wt, "add", "."); _git(wt, "commit", "-m", "docs: draft spec for #7")
    _git(wt, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    before = _git(wt, "rev-parse", "HEAD")
    res = _publish(wt)
    assert res == PublishResult(url=FOLDER_URL)
    assert _git(wt, "rev-parse", "HEAD") == before


def test_push_failure_reports_local_only_error(wt, tmp_path):
    (wt / SPEC_REL).write_text("# design\n")
    _git(wt, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))
    res = _publish(wt)
    assert res.url == ""
    assert res.error.startswith("git push failed:")
    # the commit itself survives — review from the attached session still works
    assert _git(wt, "log", "-1", "--pretty=%s") == "docs: spec folder for #7"


def test_absolute_artifact_path_is_normalized(wt, origin):
    (wt / SPEC_REL).write_text("# design\n")
    res = _publish(wt, artifact=str(wt / SPEC_REL))
    assert res.url == FOLDER_URL


def test_unusable_artifact_is_an_error_not_a_crash(wt):
    assert _publish(wt, artifact="").error != ""
    assert _publish(wt, artifact="/outside/wt.md").error != ""
    # a spec with no folder of its own would publish the whole worktree
    assert _publish(wt, artifact="spec.md").error != ""
    assert _publish(wt, artifact="../elsewhere/spec.md").error != ""


def test_dry_run_runs_no_git(wt, origin):
    (wt / SPEC_REL).write_text("# design\n")
    res = _publish(wt, dry_run=True)
    assert res.url == FOLDER_URL
    # nothing committed, nothing pushed
    assert _git(wt, "log", "-1", "--pretty=%s") == "init"
    proc = subprocess.run(["git", "-C", str(origin), "show-ref"],
                          capture_output=True, text=True)
    assert proc.stdout == ""


def test_missing_artifact_file_returns_error_not_success(wt, origin):
    """Artifact path does not exist at all — must return error, never a URL."""
    # Do NOT write the file; the directory exists but the file does not.
    res = _publish(wt)
    assert res.url == ""
    assert res.error != ""
    # Nothing pushed — origin must still be empty.
    proc = subprocess.run(["git", "-C", str(origin), "show-ref"],
                          capture_output=True, text=True)
    assert proc.stdout == ""


def test_gitignored_artifact_returns_error_not_success(wt, origin):
    """Artifact covered by .gitignore — git add silently ignores it, so
    ensure_published must detect the file is untracked and return an error
    rather than claiming a URL for a path that does not exist on GitHub."""
    # Write a .gitignore that covers the spec directory.
    (wt / ".gitignore").write_text("specs/\n")
    _git(wt, "add", ".gitignore")
    _git(wt, "commit", "-m", "chore: ignore spec dir")
    # Write the file — git will ignore it.
    (wt / SPEC_REL).write_text("# design\n")
    res = _publish(wt)
    assert res.url == ""
    assert res.error != ""
    # The gitignored file is not committed and origin stays behind.
    assert _git(wt, "log", "-1", "--pretty=%s") == "chore: ignore spec dir"


def test_published_reference_checks_remote_bytes_without_committing(wt):
    from dispatcher.spec_publish import ArtifactPublisher
    path = wt / SPEC_REL
    path.write_text('# published\n')
    _publish(wt)
    publisher = ArtifactPublisher(str(wt), BRANCH, REPO)
    reference = publisher.reference(SPEC_REL, path.read_bytes())
    url, commit = reference.url, reference.commit
    assert url == spec_url(REPO, BRANCH, SPEC_REL)
    assert commit == _git(wt, 'rev-parse', 'HEAD')
    path.write_text('# unpublished revision\n')
    assert publisher.reference(SPEC_REL, path.read_bytes()) is None
    assert _git(wt, 'rev-parse', 'HEAD') == commit
    assert _git(wt, 'status', '--porcelain', '--', SPEC_REL)


def test_artifact_publication_reuses_remote_snapshot(wt, monkeypatch):
    from dispatcher import spec_publish
    path = wt / SPEC_REL
    path.write_text('# Published\n')
    _publish(wt)
    calls = []
    git = spec_publish._git
    def record(worktree, *args):
        calls.append(args)
        return git(worktree, *args)
    monkeypatch.setattr(spec_publish, '_git', record)
    publisher = spec_publish.ArtifactPublisher(str(wt), BRANCH, REPO)
    assert publisher.reference(SPEC_REL, path.read_bytes()) is not None
    assert publisher.reference(SPEC_REL, b'unpublished') is None
    assert sum(args[0] == 'ls-remote' for args in calls) == 1
