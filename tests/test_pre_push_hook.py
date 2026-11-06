"""The committed pre-push hook refuses pushed commits that no remote ref has yet when they carry a Co-authored-by
trailer or, with commit.gpgsign on, have no good signature. conftest isolates git from the developer's config."""
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".githooks/pre-push"
ZERO = "0" * 40


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def rig(tmp_path, gpgsign):
    """A clone of a bare origin with one pushed, unsigned commit; the clone pins commit.gpgsign."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    git(repo, "push", "-q", "--no-verify", "origin", "HEAD:main")
    git(repo, "config", "commit.gpgsign", gpgsign)
    return repo


def run_hook(repo, line, remote="origin"):
    """The hook as `git push <remote>` runs it, with one ref line on stdin."""
    return subprocess.run([str(HOOK), remote, "unused-url"], cwd=repo, input=line, capture_output=True, text=True)


def commit(repo, message, *sign):
    git(repo, "commit", "-q", "--no-gpg-sign", *sign, "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def push(repo, message, *sign):
    """Commit `message`, unsigned unless `sign` adds -S, and run the hook for `git push origin HEAD:main`."""
    return run_hook(repo, f"HEAD {commit(repo, message, *sign)} refs/heads/main {git(repo, 'rev-parse', 'origin/main')}\n")


def test_a_co_authored_commit_is_refused_and_a_clean_one_passes(tmp_path):
    repo = rig(tmp_path, "false")
    assert push(repo, "fix: clean").returncode == 0
    refused = push(repo, "fix: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    assert refused.returncode == 1 and "Co-authored-by trailer" in refused.stderr


def test_a_co_authored_commit_the_remote_has_already_is_not_checked(tmp_path):
    repo = rig(tmp_path, "false")
    commit(repo, "fix: x\n\nCo-Authored-By: C <c@x>")
    git(repo, "push", "-q", "--no-verify", "origin", "HEAD:main")
    assert push(repo, "fix: clean").returncode == 0


def test_a_new_branch_pushed_to_a_url_is_checked_only_for_the_commits_no_remote_has(tmp_path):
    repo = rig(tmp_path, "false")
    commit(repo, "fix: x\n\nCo-Authored-By: C <c@x>")
    git(repo, "push", "-q", "--no-verify", "origin", "HEAD:main")
    new = commit(repo, "fix: y\n\nCo-Authored-By: D <d@x>")
    refused = run_hook(repo, f"HEAD {new} refs/heads/new {ZERO}\n", remote=str(tmp_path / "origin.git"))
    assert refused.returncode == 1 and refused.stderr == f"pre-push: {new[:7]} has a Co-authored-by trailer\n"


def test_an_unsigned_commit_is_refused_when_commit_gpgsign_is_on(tmp_path):
    refused = push(rig(tmp_path, "true"), "fix: unsigned")
    assert refused.returncode == 1 and "no good signature (N)" in refused.stderr


def test_an_unsigned_commit_passes_when_commit_gpgsign_is_off(tmp_path):
    assert push(rig(tmp_path, "false"), "fix: unsigned").returncode == 0


def test_a_signed_commit_passes_with_commit_gpgsign_on(tmp_path):
    repo = rig(tmp_path, "true")
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    (tmp_path / "allowed").write_text(f"tests@agent-ops.invalid {(tmp_path / 'key.pub').read_text()}")
    for setting, value in (("gpg.format", "ssh"), ("user.signingkey", str(key)),
                           ("gpg.ssh.allowedSignersFile", str(tmp_path / "allowed"))):
        git(repo, "config", setting, value)
    done = push(repo, "fix: signed", "-S")
    assert done.returncode == 0, done.stderr


def test_a_deleted_ref_is_not_checked(tmp_path):
    repo = rig(tmp_path, "true")
    done = run_hook(repo, f"(delete) {ZERO} refs/heads/gone {git(repo, 'rev-parse', 'HEAD')}\n")
    assert done.returncode == 0, done.stderr


def test_a_git_log_that_fails_refuses_the_push(tmp_path):
    repo = rig(tmp_path, "false")
    done = run_hook(repo, f"HEAD {'1' * 40} refs/heads/main {ZERO}\n")  # no such commit
    assert done.returncode != 0
