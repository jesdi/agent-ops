"""E2E tests for provision/with-claude-token.sh with a fake op binary.

The wrapper resolves the long-lived claude token from 1P at spawn time and
execs its argv with CLAUDE_CODE_OAUTH_TOKEN set — the token exists only in
process memory and the wrapped process's env, never on disk.
"""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = (Path(__file__).resolve().parent.parent
          / "provision" / "with-claude-token.sh")
PROBE = ["sh", "-c", 'echo "tok=${CLAUDE_CODE_OAUTH_TOKEN-UNSET}"']


@pytest.fixture
def rig(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "op-token.env").write_text(
        "OP_SERVICE_ACCOUNT_AGENT_OPS_TOKEN=svc-tok\n")

    calls = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    op = bin_dir / "op"
    op.write_text(f"""#!/bin/sh
echo "op $@ token=$OP_SERVICE_ACCOUNT_TOKEN" >> "{calls}"
case "$*" in
  *agent-ops-claude/CLAUDE_CODE_OAUTH_TOKEN*)
    [ -f "{tmp_path}/TOKEN_ABSENT" ] && exit 1
    [ -f "{tmp_path}/TOKEN_EMPTY" ] && {{ echo ""; exit 0; }}
    echo "FAKE_LONG_LIVED_TOKEN" ;;
  *) exit 1 ;;
esac
""")
    op.chmod(0o755)

    env = dict(os.environ,
               AGENT_OPS_STATE_DIR=str(state),
               AGENT_OPS_OP=str(op))
    env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return SimpleNamespace(tmp=tmp_path, state=state, calls=calls, env=env)


def run(rig, argv=PROBE):
    return subprocess.run(["bash", str(SCRIPT)] + argv, env=rig.env,
                          capture_output=True, text=True)


def test_wrapped_command_receives_token_from_1p(rig):
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert "tok=FAKE_LONG_LIVED_TOKEN" in r.stdout


def test_op_authenticates_by_sourcing_op_token_env(rig):
    # tmux pane shells (session spawns) have no OP_SERVICE_ACCOUNT_TOKEN;
    # the wrapper sources the state dir's op-token.env itself.
    run(rig)
    assert "token=svc-tok" in rig.calls.read_text()


def test_token_absent_runs_command_without_the_var(rig):
    (rig.tmp / "TOKEN_ABSENT").touch()
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert "tok=UNSET" in r.stdout


def test_empty_token_runs_command_without_the_var(rig):
    (rig.tmp / "TOKEN_EMPTY").touch()
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert "tok=UNSET" in r.stdout


def test_missing_op_token_file_degrades_to_plain_exec(rig):
    (rig.state / "op-token.env").unlink()
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert "tok=UNSET" in r.stdout


def test_wrapped_command_exit_status_is_preserved(rig):
    r = run(rig, ["sh", "-c", "exit 7"])
    assert r.returncode == 7
