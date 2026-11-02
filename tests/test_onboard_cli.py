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
        self.kicked_with = []  # the intent actions waiting at each kick

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append(argv)
        assert " ".join(argv) == KICK
        self.kicked_with.append([p.stem.rsplit("-", 1)[1] for p in sorted(
            (onboard.Path(onboard.config.load_config(self.config).state_dir)
             / "onboarding-intents").glob("*.json"))])
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


ADD = ("analytics", "jesdi/analytics", "--board", "jesdi/4")


def test_add_shows_the_proposal_and_confirms_its_revision(tmp_path, box, monkeypatch, capsys):
    stdin(monkeypatch, "\nmake gate\n\ny\n")
    sh = Box(box)
    add(box, sh, *ADD)
    rec = record(tmp_path)
    assert sh.kicked_with == [["add"], ["confirm"]]
    assert rec["confirmed_revision"] == rec["revision"]
    out = capsys.readouterr().out
    assert "repo: jesdi/analytics" in out
    assert f"confirm {rec['revision'][:12]}? [y/N]" in out


def test_add_without_a_yes_leaves_it_unconfirmed(tmp_path, box, monkeypatch):
    stdin(monkeypatch, "\nmake gate\n\n\n")
    sh = Box(box)
    add(box, sh, *ADD)
    assert sh.kicked_with == [["add"]]
    assert record(tmp_path)["confirmed_revision"] == ""


def test_a_rerun_offers_the_current_commands(tmp_path, box, monkeypatch, capsys):
    """Enter keeps each command, so the same request resumes."""
    stdin(monkeypatch, "make setup\nmake gate\n\nn\n")
    add(box, Box(box), *ADD)
    stdin(monkeypatch, "\n\n\ny\n")
    sh = Box(box)
    add(box, sh, "analytics", "https://github.com/jesdi/Analytics", "--board", "jesdi/4")
    assert sh.kicked_with == [["add"], ["confirm"]]
    assert "jesdi/Analytics gate_cmd [make gate]: " in capsys.readouterr().out
    rec = record(tmp_path)
    assert rec["request"]["commands"][0]["setup_cmd"] == "make setup"
    assert rec["confirmed_revision"] == rec["revision"]


def test_a_rerun_with_other_repositories_before_confirm_edits(tmp_path, box, monkeypatch):
    stdin(monkeypatch, "\nmake gate\n\nn\n")
    add(box, Box(box), *ADD)
    stdin(monkeypatch, "\n\n\n\nnpm test\n\nn\n")
    sh = Box(box, FakeSh({"gh api repos/jesdi/web": json.dumps(
        {"id": 303, "full_name": "jesdi/web", "name": "web"})}))
    add(box, sh, "analytics", "jesdi/analytics", "jesdi/web", "--board", "jesdi/4")
    assert sh.kicked_with == [["add"]]
    assert record(tmp_path)["request"]["repos"] == ["jesdi/analytics", "jesdi/web"]


def test_a_refused_rerun_shows_the_engines_reason(tmp_path, box, monkeypatch, capsys):
    stdin(monkeypatch, "\nmake gate\n\ny\n")
    add(box, Box(box), *ADD)
    stdin(monkeypatch, "\nnpm test\n\n")
    sh = Box(box)
    assert add(box, sh, "analytics", "jesdi/web", "--board", "jesdi/4") == 0
    assert sh.kicked_with == [["add"]]
    assert "notice: request refused" in capsys.readouterr().out
    assert record(tmp_path)["request"]["repos"] == ["jesdi/analytics"]


def test_a_rerun_of_a_complete_onboarding_says_so(tmp_path, box, monkeypatch, capsys):
    done = onboard.Record("analytics", status="complete", request={
        "repos": ["jesdi/analytics"], "board": {"owner": "jesdi", "number": 4},
        "commands": [{"setup_cmd": "", "gate_cmd": "make gate", "verify_cmd": ""}]},
        steps=[onboard.Step(n, onboard.StepState.VERIFIED) for n in onboard.STEPS])
    onboard.write_json_atomic(state(tmp_path) / "onboarding" / "analytics.json",
                              onboard.asdict(done))
    stdin(monkeypatch, "\n\n\n")
    assert add(box, Box(box), *ADD) == 0
    out = capsys.readouterr().out
    assert "notice: already onboarded" in out and "visible" in out


def test_agent_ops_is_the_console_script_of_project_add():
    import tomllib
    from pathlib import Path
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["agent-ops"] == "dispatcher.onboard_cli:main"


def test_the_unit_runs_the_engine_once_from_its_module(tmp_path, box):
    onboard.main(["--config", str(box)])
    assert (state(tmp_path) / "onboarding.lock").exists()
