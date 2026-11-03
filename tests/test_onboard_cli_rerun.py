"""`agent-ops project add` on a completed onboarding: an informational,
prompt-free rerun. Black box through dispatcher.onboard_cli.main."""
import getpass
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dispatcher import onboard, onboard_cli
from tests.test_onboard import FakeSh, NOW, box, no_get, record, state  # noqa: F401

ADD = ("analytics", "jesdi/analytics", "--board", "jesdi/4")
ROOT = Path(__file__).resolve().parent.parent
REQUEST = {"repos": ["jesdi/analytics"], "board": {"owner": "jesdi", "number": 4},
           "commands": [{"setup_cmd": "", "gate_cmd": "make gate", "verify_cmd": ""}]}
PROMPTS = ("setup_cmd", "gate_cmd", "verify_cmd", "stdin ended", "[y/N]")


def assert_prompt_free(text):
    """No command or confirmation prompt; the recorded step `confirm` is fine."""
    for p in PROMPTS:
        assert p not in text
    assert not re.search(r"^confirm .*\?", text, re.M)
    for step in onboard.STEPS:
        assert step in text


@pytest.fixture(autouse=True)
def as_agent(monkeypatch):
    monkeypatch.setattr(getpass, "getuser", lambda: "agent")


class Stdin(io.StringIO):
    """Counts what is read from it."""
    reads = 0

    def readline(self, *a):
        self.reads += 1
        return super().readline(*a)


class Kick:
    """The CLI's sh: records the call, then runs one real engine run."""

    def __init__(self, config):
        self.config, self.calls = config, []

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append(argv)
        onboard.run(self.config, sh=FakeSh(), get=no_get, now=NOW)
        return ""


def stdin(monkeypatch, text):
    s = Stdin(text)
    monkeypatch.setattr(sys, "stdin", s)
    return s


def run(box, sh, *args):
    return onboard_cli.main(["project", "add", *args, "--config", str(box)], sh=sh)


def write_complete(tmp_path, notice=""):
    done = onboard.Record("analytics", status="complete", request=REQUEST, notice=notice,
                          steps=[onboard.Step(n, onboard.StepState.VERIFIED)
                                 for n in onboard.STEPS])
    path = state(tmp_path) / "onboarding" / "analytics.json"
    onboard.write_json_atomic(path, onboard.asdict(done))
    return path


def intent_files(tmp_path):
    return sorted((state(tmp_path) / "onboarding-intents").glob("*"))


def test_complete_rerun_with_closed_stdin_reports_status_and_succeeds(
        tmp_path, box, monkeypatch, capsys):
    write_complete(tmp_path)
    stdin(monkeypatch, "")
    assert run(box, Kick(box), *ADD) == 0
    cap = capsys.readouterr()
    assert "analytics: complete" in cap.out
    assert "visible" in cap.out and "notice: already onboarded" in cap.out
    assert_prompt_free(cap.out + cap.err)


def test_complete_rerun_as_a_subprocess_with_closed_stdin(tmp_path, box):
    path = write_complete(tmp_path)
    before = path.read_bytes()
    kicks = tmp_path / "kicks"
    code = ("import sys\nfrom dispatcher import onboard_cli\n"
            f"def sh(argv, cwd=None, env=None):\n    open({str(kicks)!r}, 'a').write('kick')\n    return ''\n"
            "sys.exit(onboard_cli.main(sys.argv[1:], sh=sh))")
    r, w = os.pipe()
    os.close(w)  # the write end is gone: stdin is at EOF
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, "project", "add", *ADD, "--config", str(box)],
            cwd=ROOT, env={**os.environ, "USER": "agent", "LOGNAME": "agent",
                           "PYTHONPATH": str(ROOT)},
            stdin=r, capture_output=True, text=True, timeout=60)
    finally:
        os.close(r)
    out, err = proc.stdout, proc.stderr
    assert proc.returncode == 0, err or out
    assert "analytics: complete" in out and "notice: already onboarded" in out
    assert "visible" in out
    assert_prompt_free(out + err)
    assert not kicks.exists() and path.read_bytes() == before
    assert not intent_files(tmp_path)


def test_complete_rerun_leaves_input_unread_and_state_unchanged(
        tmp_path, box, monkeypatch, capsys):
    path = write_complete(tmp_path, notice="stale: waiting on something old")
    before = path.read_bytes()
    supplied = stdin(monkeypatch, "make setup\nmake gate\nmake e2e\ny\n")
    sh = Kick(box)
    assert run(box, sh, *ADD) == 0
    out = capsys.readouterr().out
    assert "analytics: complete" in out and "notice: already onboarded" in out
    assert "stale" not in out
    assert supplied.reads == 0
    assert path.read_bytes() == before
    assert not intent_files(tmp_path)
    assert sh.calls == []


def test_new_onboarding_collects_commands_and_stays_unconfirmed(
        tmp_path, box, monkeypatch, capsys):
    stdin(monkeypatch, "make setup\nmake gate\nmake e2e\nn\n")
    assert run(box, Kick(box), *ADD) == 0
    rec = record(tmp_path)
    assert rec["request"]["commands"] == [
        {"setup_cmd": "make setup", "gate_cmd": "make gate", "verify_cmd": "make e2e"}]
    assert rec["confirmed_revision"] == ""
    assert "confirm" in capsys.readouterr().out


def test_open_rerun_offers_saved_defaults_and_confirms_the_shown_revision(
        tmp_path, box, monkeypatch, capsys):
    stdin(monkeypatch, "make setup\nmake gate\nmake e2e\nn\n")
    run(box, Kick(box), *ADD)
    capsys.readouterr()
    stdin(monkeypatch, "\n\n\ny\n")
    assert run(box, Kick(box), *ADD) == 0
    out = capsys.readouterr().out
    assert "setup_cmd [make setup]" in out and "gate_cmd [make gate]" in out
    rec = record(tmp_path)
    assert rec["confirmed_revision"] == rec["revision"] != ""
    assert f"confirm {rec['revision'][:12]}?" in out


@pytest.mark.parametrize("opened", [False, True], ids=["missing", "open"])
def test_closed_stdin_still_fails_for_unfinished_commands(
        tmp_path, box, monkeypatch, opened):
    if opened:
        stdin(monkeypatch, "make setup\nmake gate\nmake e2e\nn\n")
        run(box, Kick(box), *ADD)
    sh = Kick(box)
    stdin(monkeypatch, "")
    with pytest.raises(SystemExit, match="stdin ended before every command was given"):
        run(box, sh, *ADD)
    assert sh.calls == []
    assert not intent_files(tmp_path)


def test_a_completed_record_does_not_bypass_the_account_check(
        tmp_path, box, monkeypatch, capsys):
    monkeypatch.setattr(getpass, "getuser", lambda: "jesdi")
    path = write_complete(tmp_path)
    before = path.read_bytes()
    supplied = stdin(monkeypatch, "")
    sh = Kick(box)
    assert run(box, sh, *ADD) == 2
    assert "run agent-ops as agent" in capsys.readouterr().err
    assert supplied.reads == 0 and sh.calls == []
    assert path.read_bytes() == before and not intent_files(tmp_path)
