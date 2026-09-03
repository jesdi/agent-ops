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
def _no_ambient_claude_token(monkeypatch):
    """Session containers carry CLAUDE_CODE_OAUTH_TOKEN (podman --env-file),
    and budget.fetch_usage prefers it over any credentials_path fixture — an
    ambient token would flip every Authorization assertion in the suite.
    Tests exercising the env path set it explicitly. OP_SERVICE_ACCOUNT_TOKEN
    likewise: with it present, budget.fetch_usage would shell out to the real
    `op` binary mid-suite."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point AGENT_OPS_STATE_DIR at a per-test tmp dir so no test can ever
    read or write the box's real state (claude-home seeding in
    create_workspace writes files there). Tests that care about the exact
    value still override it explicitly."""
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_herdr_server(monkeypatch):
    """The dev machine runs a real herdr server. Every herdr call in the
    suite must be an explicit fake: default to "no server" so an unstubbed
    call degrades (None/False) instead of creating tabs on the developer's
    desktop. Tests that exercise herdr monkeypatch herdr._run themselves,
    which takes precedence because it is applied after this fixture."""
    from dispatcher import herdr
    monkeypatch.setattr(herdr, "_run", lambda args: None)
