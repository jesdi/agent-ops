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
  *agent-ops-claude/CLAUDE_CODE_OAUTH_TOKEN*)
    if [ -f "{tmp_path}/CLAUDE_TOKEN_EMPTY" ]; then echo ""; exit 0; fi
    [ -f "{tmp_path}/CLAUDE_TOKEN" ] || exit 1
    echo "FAKE_LONG_LIVED_TOKEN" ;;
  *agent-ops-claude*)
    [ -f "{tmp_path}/CLAUDE_CREDS" ] || exit 1
    echo '{{"fake": "claude-creds"}}' ;;
  *agent-ops-git-signing/public*)
    [ -f "{tmp_path}/SIGNING_KEY" ] || exit 1
    echo "ssh-ed25519 AAAAFAKE agent-ops-box" ;;
  *agent-ops-git-signing/private*)
    [ -f "{tmp_path}/SIGNING_KEY" ] || exit 1
    echo "FAKE_OPENSSH_PRIVATE_KEY" ;;
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

    home = tmp_path / "home"
    home.mkdir()

    env = dict(
        os.environ,
        AGENT_OPS_STATE_DIR=str(state),
        AGENT_OPS_OP=str(op),
        AGENT_OPS_GH=str(gh),
        HOME=str(home),
    )
    env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    return SimpleNamespace(tmp=tmp_path, state=state, calls=calls,
                           stdin=stdin_log, home=home, env=env)


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


def git_config(rig, key):
    r = subprocess.run(["git", "config", "--global", "--get", key],
                       env=rig.env, capture_output=True, text=True)
    return r.stdout.strip()


def test_git_signing_key_materialized_and_git_configured(rig):
    (rig.tmp / "SIGNING_KEY").write_text("")
    r = run(rig)
    assert r.returncode == 0, r.stderr

    key = rig.state / "git-signing-key"
    assert key.read_text().strip() == "FAKE_OPENSSH_PRIVATE_KEY"
    assert (key.stat().st_mode & 0o777) == 0o600
    pub = rig.state / "git-signing-key.pub"
    assert pub.read_text().strip() == "ssh-ed25519 AAAAFAKE agent-ops-box"

    assert ("op read op://agent-ops/agent-ops-git-signing/private key"
            "?ssh-format=openssh") in calls(rig)
    assert git_config(rig, "gpg.format") == "ssh"
    assert git_config(rig, "user.signingkey") == str(key)
    assert git_config(rig, "commit.gpgsign") == "true"
    assert git_config(rig, "tag.gpgsign") == "true"


def test_git_signing_key_absent_skips_signing_config_not_fatal(rig):
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert not (rig.state / "git-signing-key").exists()
    assert git_config(rig, "commit.gpgsign") == ""
    assert "git-signing" in (r.stdout + r.stderr).lower()


def test_claude_long_lived_token_materialized_to_env_file(rig):
    # The 1P-backed `claude setup-token` output becomes claude-token.env —
    # the single static-token source the units (EnvironmentFile) and the
    # session containers (podman --env-file) all read, replacing the
    # refresh-rotation race on claude-home/.credentials.json.
    (rig.tmp / "CLAUDE_TOKEN").touch()
    r = run(rig)
    assert r.returncode == 0, r.stderr
    env_file = rig.state / "claude-token.env"
    assert env_file.read_text() == (
        "CLAUDE_CODE_OAUTH_TOKEN=FAKE_LONG_LIVED_TOKEN\n")
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_missing_long_lived_token_prints_setup_token_guidance(rig):
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert not (rig.state / "claude-token.env").exists()
    assert "setup-token" in r.stdout


def test_empty_long_lived_token_field_writes_no_env_file(rig):
    # An empty-but-present 1P field must not inject an empty
    # CLAUDE_CODE_OAUTH_TOKEN into every container.
    (rig.tmp / "CLAUDE_TOKEN_EMPTY").touch()
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert not (rig.state / "claude-token.env").exists()


def test_stale_token_env_file_kept_with_warning_when_field_gone(rig):
    # Never auto-delete (a transient 1P outage would drop a working token),
    # but say the file was left so a revoked token gets cleaned up by hand.
    (rig.state / "claude-token.env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=old\n")
    r = run(rig)
    assert r.returncode == 0, r.stderr
    assert (rig.state / "claude-token.env").exists()
    assert "left in place" in r.stdout
