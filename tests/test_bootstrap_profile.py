"""The bootstrap profile snippet must land CLAUDE_CONFIG_DIR in ~/.profile
exactly once, no matter how many times bootstrap re-runs (it is documented
as idempotent). The snippet is extracted from bootstrap.sh by its markers
and executed against a sandbox HOME — bootstrap itself is unrunnable in CI
(installs packages, clones repos)."""
import re
import subprocess
from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parent.parent / "provision" / "bootstrap.sh"


def _snippet() -> str:
    text = BOOTSTRAP.read_text()
    m = re.search(
        r"# >>> agent-ops claude-config >>>\n(.*?)# <<< agent-ops claude-config <<<",
        text, re.S)
    assert m, "claude-config block missing from bootstrap.sh"
    return m.group(1)


def _run(snippet: str, home: Path):
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", snippet],
        check=True, env={"AGENT_HOME": str(home), "PATH": "/usr/bin:/bin"})


def test_profile_export_is_added_once_across_reruns(tmp_path):
    snippet = _snippet()
    _run(snippet, tmp_path)
    _run(snippet, tmp_path)  # idempotent re-run
    profile = (tmp_path / ".profile").read_text()
    line = 'export CLAUDE_CONFIG_DIR="$HOME/agent-ops-state/claude-home"'
    assert profile.count(line) == 1


def test_profile_without_trailing_newline_is_not_corrupted(tmp_path):
    """A .profile whose last byte is not a newline would swallow the export
    onto the previous line: the joined line breaks the pre-existing command
    AND defeats grep -qxF's whole-line match, so every re-run appends again."""
    snippet = _snippet()
    (tmp_path / ".profile").write_text('umask 022\nexport PATH="$HOME/bin:$PATH"')
    _run(snippet, tmp_path)
    _run(snippet, tmp_path)  # idempotent even on the unterminated file
    profile = (tmp_path / ".profile").read_text()
    line = 'export CLAUDE_CONFIG_DIR="$HOME/agent-ops-state/claude-home"'
    assert profile.count(line) == 1
    # On its own line, and the pre-existing content survived intact.
    lines = profile.splitlines()
    assert line in lines
    assert 'umask 022' in lines
    assert 'export PATH="$HOME/bin:$PATH"' in lines
