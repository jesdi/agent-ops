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
    and usage_providers.AnthropicUsage.fetch prefers it over any
    credentials_path fixture — an ambient token would flip every Authorization
    assertion in the suite. Tests exercising the env path set it explicitly.
    OP_SERVICE_ACCOUNT_TOKEN likewise: with it present,
    usage_providers.AnthropicUsage.fetch would shell out to the real `op`
    binary mid-suite."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_OPS_COMMAND_WRAPPER", raising=False)
    # The host store is a usage-token fallback too; the dev machine's real
    # ~/.claude must not leak in. Tests exercising it set it explicitly.
    from dispatcher import usage_providers
    monkeypatch.setattr(usage_providers, "HOST_CREDENTIALS",
                        str(Path(__file__).parent / "no-host-credentials.json"))


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point AGENT_OPS_STATE_DIR at a per-test tmp dir so no test can ever
    read or write the box's real state (claude-home seeding in
    create_workspace writes files there). Tests that care about the exact
    value still override it explicitly."""
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))


class _EveryTarget(dict):
    """A clones mapping that answers every target with one path."""

    def __init__(self, clone: str):
        super().__init__()
        self.clone = clone

    def get(self, key, default=None):
        return self.clone


@pytest.fixture(autouse=True)
def _a_clone_for_every_target(tmp_path, monkeypatch):
    """Production builds Sessions with the configured clone of each target
    and launches nothing for a target without one. A test that builds
    `Sessions()` with no `clones` gets <tmp_path>/clone for every target, so
    launch tests need not repeat it; a test of the rule passes `clones`."""
    from dispatcher import sessions
    real = sessions.Sessions.__init__

    def init(self, *args, clones=None, **kw):
        real(self, *args, clones=_EveryTarget(str(tmp_path / "clone"))
             if clones is None else clones, **kw)
    monkeypatch.setattr(sessions.Sessions, "__init__", init)


@pytest.fixture(autouse=True)
def _no_herdr_server(monkeypatch):
    """The dev machine runs a real herdr server. Every herdr call in the
    suite must be an explicit fake: default to "no server" so an unstubbed
    call degrades (None/False) instead of creating tabs on the developer's
    desktop. Tests that exercise herdr monkeypatch herdr._run themselves,
    which takes precedence because it is applied after this fixture."""
    from dispatcher import herdr
    monkeypatch.setattr(herdr, "_run", lambda args: None)


@pytest.fixture(autouse=True)
def codex_package(tmp_path, monkeypatch) -> Path:
    """Every test gets a home holding a Codex package: containers refuse a
    Codex binary outside one, and CI has no ~/.local/bin/codex while a dev
    machine has a real one. Returns the package root, which is what a Codex
    session mounts. Tests that need another home set their own, which wins
    because it is applied after this fixture."""
    home = tmp_path / "isolated-home"
    package = home / ".local" / "lib" / "codex" / "1.2.3"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "codex").write_text("")
    (package / "codex-package.json").write_text("{}")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "codex").symlink_to(package / "bin" / "codex")
    monkeypatch.setattr(Path, "home", lambda: home)
    return package
