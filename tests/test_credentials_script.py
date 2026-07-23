"""E2E tests for provision/credentials.sh with fake op/gh binaries."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "provision" / "credentials.sh"


@pytest.fixture
def rig(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "op-token.env").write_text(
        "OP_SERVICE_ACCOUNT_AGENT_OPS_TOKEN=svc-tok\n")

    calls = tmp_path / "calls.log"
    stdin_log = tmp_path / "stdin.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # op read: repo token always resolves; claude credentials only when
    # the CLAUDE_CREDS marker file exists (simulates the 1P item existing).
    op = bin_dir / "op"
    op.write_text(f"""#!/bin/sh
echo "op $@ token=$OP_SERVICE_ACCOUNT_TOKEN" >> "{calls}"
case "$*" in
  *agent-ops-github/GH_REPO_TOKEN*) echo "FAKE_REPO_PAT" ;;
  *agent-ops-claude*)
    [ -f "{tmp_path}/CLAUDE_CREDS" ] || exit 1
    echo '{{"fake": "claude-creds"}}' ;;
  *) exit 1 ;;
esac
""")
    op.chmod(0o755)

    gh = bin_dir / "gh"
    gh.write_text(f"""#!/bin/sh
echo "gh $@" >> "{calls}"
[ -t 0 ] || cat >> "{stdin_log}"
""")
    gh.chmod(0o755)

    env = dict(
        os.environ,
        AGENT_OPS_STATE_DIR=str(state),
        AGENT_OPS_OP=str(op),
        AGENT_OPS_GH=str(gh),
    )
    env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    return SimpleNamespace(tmp=tmp_path, state=state, calls=calls,
                           stdin=stdin_log, env=env)


def run(rig):
    return subprocess.run(["bash", str(SCRIPT)], env=rig.env,
                          capture_output=True, text=True)


def calls(rig):
    return rig.calls.read_text() if rig.calls.exists() else ""


def test_gh_login_receives_repo_token_from_1password(rig):
    r = run(rig)
    assert r.returncode == 0, r.stderr
    log = calls(rig)
    assert "op read op://agent-ops/agent-ops-github/GH_REPO_TOKEN" in log
    assert "gh auth login --with-token" in log
    assert rig.stdin.read_text().strip() == "FAKE_REPO_PAT"


def test_op_runs_with_remapped_service_account_token(rig):
    run(rig)
    assert "token=svc-tok" in calls(rig)


def test_missing_op_token_file_fails_before_any_call(rig):
    (rig.state / "op-token.env").unlink()
    r = run(rig)
    assert r.returncode != 0
    assert calls(rig) == ""
    assert "op-token.env" in r.stderr


def test_claude_credentials_restored_when_backed_up(rig):
    (rig.tmp / "CLAUDE_CREDS").write_text("")
    r = run(rig)
    assert r.returncode == 0, r.stderr
    creds = rig.state / "claude-home" / ".credentials.json"
    assert creds.read_text().strip() == '{"fake": "claude-creds"}'
    assert (creds.stat().st_mode & 0o777) == 0o600


def test_claude_credentials_absent_is_not_fatal(rig):
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert not (rig.state / "claude-home" / ".credentials.json").exists()
    assert "claude" in (r.stdout + r.stderr).lower()
