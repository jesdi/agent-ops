"""Tracked source files must stay under MAX_LINES (tests/test_file_length.py).

Long files make an agent read far more than the code it needs; split them into
deep modules instead of raising the limit.
"""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MAX_LINES = 1000
SUFFIXES = (".py", ".ts", ".tsx")
# Generated: never split by hand.
EXEMPT = {"frontend/src/lib/api-types.ts"}
# Over the limit when the check landed; each is a follow-up split. Remove an
# entry once its file is under the limit (the check fails on a stale entry).
ALLOWED = {
    "dispatcher/onboard.py",
    "dispatcher/lifecycle.py",
    "web/read_model.py",
    "web/app.py",
    "tests/test_web_routes_read.py",
    "tests/test_web_read_model.py",
    "tests/test_web_sources.py",
    "tests/test_onboard.py",
}


def violations(lengths: dict[str, int]) -> list[str]:
    """One message per file over the limit, and per allowlist entry no longer needed."""
    over = {path for path, n in lengths.items() if n > MAX_LINES} - EXEMPT
    found = [f"{path}: {lengths[path]} lines (max {MAX_LINES})"
             for path in sorted(over - ALLOWED)]
    found += [f"{path}: in ALLOWED but no longer over {MAX_LINES} lines; drop it"
              for path in sorted(ALLOWED - over)]
    return found


def tracked_lengths() -> dict[str, int]:
    paths = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                           capture_output=True, text=True).stdout.split("\0")
    return {p: len((ROOT / p).read_text().splitlines())
            for p in paths if p.endswith(SUFFIXES) and (ROOT / p).is_file()}

