"""`agent-ops project add` (dispatcher.onboard_cli): prompts, intents, the
kick and the record printout. The kick runs one engine run against fakes."""
import getpass
import io
import json
import sys

import pytest

from dispatcher import onboard, onboard_cli
from tests.test_onboard import NOW, FakeSh, box, no_get, record, state  # noqa: F401

KICK = "systemctl --user start agent-ops-onboard.service"


@pytest.fixture(autouse=True)
def as_agent(monkeypatch):
    monkeypatch.setattr(getpass, "getuser", lambda: "agent")


def stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


class Box:
    """The CLI's sh: the kick runs one engine run with `engine` as its sh."""

    def __init__(self, config, engine=None):
        self.config, self.engine, self.calls = config, engine or FakeSh(), []

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append(argv)
        assert " ".join(argv) == KICK
        onboard.run(self.config, sh=self.engine, get=no_get, now=NOW)
        return ""


def add(box, sh, *args):
    return onboard_cli.main(["project", "add", *args, "--config", str(box)], sh=sh)


def intents(tmp_path):
    return sorted(p.name for p in (state(tmp_path) / "onboarding-intents").glob("*"))


def test_add_prompts_for_each_repositorys_commands_starts_and_kicks(
        tmp_path, box, monkeypatch, capsys):
    stdin(monkeypatch, "make setup\nmake gate\nmake e2e\n\nnpm test\n\n")
    sh = Box(box, FakeSh({"gh api repos/jesdi/web": json.dumps(
        {"id": 303, "full_name": "jesdi/web", "name": "web"})}))
    assert add(box, sh, "analytics", "jesdi/analytics", "jesdi/web",
               "--board", "jesdi/4") == 0
    assert sh.calls == [KICK.split()]
    rec = record(tmp_path)
    assert rec["request"] == {
        "repos": ["jesdi/analytics", "jesdi/web"], "board": {"owner": "jesdi", "number": 4},
        "commands": [{"setup_cmd": "make setup", "gate_cmd": "make gate",
                      "verify_cmd": "make e2e"},
                     {"setup_cmd": "", "gate_cmd": "npm test", "verify_cmd": ""}]}
    out = capsys.readouterr().out
    assert "jesdi/analytics setup_cmd" in out
    assert "analytics: open" in out and "resolve" in out


def test_add_asks_again_for_an_empty_gate_cmd(tmp_path, box, monkeypatch):
    stdin(monkeypatch, "\n\nmake gate\n\n")
    add(box, Box(box), "analytics", "jesdi/analytics", "--board", "jesdi/4")
    assert record(tmp_path)["request"]["commands"][0]["gate_cmd"] == "make gate"


def test_add_refuses_another_user(tmp_path, box, monkeypatch, capsys):
    monkeypatch.setattr(getpass, "getuser", lambda: "jesdi")
    assert add(box, Box(box), "analytics", "jesdi/analytics", "--board", "jesdi/4") == 2
    assert "agent" in capsys.readouterr().err
    assert record(tmp_path) is None


@pytest.mark.parametrize("args", [
    ["Bad_Name", "jesdi/analytics", "--board", "jesdi/4"],
    ["analytics", "jesdi/analytics", "--board", "jesdi"],
    ["analytics", "jesdi/analytics", "--board", "jesdi/four"],
])
def test_add_refuses_a_bad_name_or_board(tmp_path, box, args):
    with pytest.raises(SystemExit):
        add(box, Box(box), *args)
    assert not (state(tmp_path) / "onboarding-intents").exists()


def test_add_stops_when_stdin_ends_early(tmp_path, box, monkeypatch):
    stdin(monkeypatch, "make setup\n")
    with pytest.raises(SystemExit, match="stdin ended"):
        add(box, Box(box), "analytics", "jesdi/analytics", "--board", "jesdi/4")
    assert record(tmp_path) is None
