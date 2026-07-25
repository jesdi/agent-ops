"""Ensure the checkout under test is imported, not another copy.

An editable install of agent-ops (pip install -e) registers an import
finder that resolves `dispatcher` to wherever it was installed from.
When tests run from a worktree via an entrypoint that doesn't put the
cwd on sys.path (bare `pytest`), that finder silently wins and the
wrong code gets tested. Prepending this checkout's root pins imports
to the tree the tests live in.
"""
import sys
from pathlib import Path

import pytest

root = str(Path(__file__).resolve().parent.parent)
if root not in sys.path:
    sys.path.insert(0, root)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point AGENT_OPS_STATE_DIR at a per-test tmp dir so no test can ever
    read or write the box's real state (claude-home seeding in
    create_workspace writes files there). Tests that care about the exact
    value still override it explicitly."""
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
