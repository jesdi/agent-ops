"""Test-only Git dispatch: real Git for this test's disposable repositories."""
import os
from pathlib import Path
import subprocess
import sys


def executable(argv, root, guarded, real):
    """Resolve the actual common repository, including linked worktree metadata."""
    end = 0
    while end < len(argv) and argv[end].startswith("-"):
        end += 2 if argv[end] in {
            "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"
        } else 1
    probe = subprocess.run(
        [real, *argv[:end], "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if probe.returncode == 0 and Path(probe.stdout.strip()).resolve().is_relative_to(root):
        return real
    return guarded


if __name__ == "__main__":
    chosen = executable(
        sys.argv[1:], Path(os.environ["FIXTURE_GIT_ROOT"]).resolve(),
        os.environ["FIXTURE_GIT_GUARDED"], os.environ["FIXTURE_GIT_REAL"],
    )
    os.execv(chosen, [chosen, *sys.argv[1:]])
