"""The onboarding engine (dispatcher.onboard): one run drains the onboarding
intents and moves every open record as far as it goes. Every subprocess goes
through the fake `sh`, every HTTP request through the fake `get`."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dispatcher import onboard

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)

LIVE = """\
state_dir: /ignored/the/env/wins
projects:
  - name: portfolio-eval
    board: {owner: jesdi, number: 1}
    repositories:
      - name: portfolio_eval
        repo: jesdi/portfolio_eval
        github_id: 101
        clone_path: /home/agent/repos/portfolio_eval
        worktrees_path: /home/agent/repos/portfolio_eval.worktrees
        gate_cmd: make gate
"""

BOARD = json.dumps({"data": {"repositoryOwner": {"projectV2": {
    "id": "PVT_4", "fields": {"nodes": []}}}}})
NO_BOARD = json.dumps({"data": {"repositoryOwner": None}})


def gh_repo(github_id, full_name):
    return json.dumps({"id": github_id, "full_name": full_name,
                       "name": full_name.split("/")[1]})


def failure(stderr):
    return subprocess.CalledProcessError(1, ["x"], output="", stderr=stderr)


def cloned(argv, cwd, env):
    """`gh repo clone <repo> <path>`: the clone appears at <path>."""
    Path(argv[-1]).mkdir(parents=True)
    (Path(argv[-1]) / ".git").mkdir()
    return ""


ORIGIN = "https://github.com/jesdi/analytics.git\n"


class FakeSh:
    """Answers each call from the first route whose key is a substring of
    the joined argv: a string, an exception to raise, or a callable taking
    (argv, cwd, env). An unrouted call fails the test."""

    def __init__(self, routes=None):
        self.routes = {"gh api repos/jesdi/analytics": gh_repo(202, "jesdi/analytics"),
                       "gh api graphql": BOARD,
                       "gh repo clone": cloned,
                       "remote get-url origin": ORIGIN, **(routes or {})}
        self.calls = []

    def route(self, key, answer):
        self.routes = {key: answer, **self.routes}

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append((argv, cwd, env))
        line = " ".join(argv)
        for key, answer in self.routes.items():
            if key in line:
                if callable(answer):
                    answer = answer(argv, cwd, env)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected sh call: {line}")

    def ran(self, key):
        return [c for c in self.calls if key in " ".join(c[0])]


def no_get(url, headers):
    raise AssertionError(f"unexpected GET {url}")


@pytest.fixture
def box(tmp_path):
    """The live config and the state dir the conftest points the loader at."""
    p = tmp_path / "targets.yaml"
    p.write_text(LIVE)
    return p


def state(tmp_path) -> Path:
    return tmp_path / "state"


def start(tmp_path, project="analytics", repos=("jesdi/analytics",),
          board=None, commands=None, epoch_ms=1000):
    return onboard.write_intent(state(tmp_path), "start", project, {
        "repos": list(repos), "board": board or {"owner": "jesdi", "number": 4},
        "commands": commands or [{"setup_cmd": "", "gate_cmd": "make gate",
                                  "verify_cmd": ""} for _ in repos]},
        actor="agent", epoch_ms=epoch_ms)


def run(box, sh, get=no_get, now=NOW):
    onboard.run(box, sh=sh, get=get, now=now)


def record(tmp_path, project="analytics"):
    return onboard.load_record(state(tmp_path), project)


def step(rec, name):
    return next(s for s in rec["steps"] if s["name"] == name)


# -- intents ------------------------------------------------------------------


def test_start_intent_opens_a_record_and_is_deleted(tmp_path, box):
    p = start(tmp_path)
    assert p.parent == state(tmp_path) / "onboarding-intents"
    assert p.name == "1000-analytics-start.json"
    run(box, FakeSh())
    rec = record(tmp_path)
    assert (rec["project"], rec["started_by"]) == ("analytics", "agent")
    assert rec["request"]["repos"] == ["jesdi/analytics"]
    assert not p.exists()


def test_a_run_holds_the_onboarding_lock_not_the_pass_lock(tmp_path, box):
    run(box, FakeSh())
    assert (state(tmp_path) / "onboarding.lock").exists()
    assert not (state(tmp_path) / "convergence.lock").exists()


def test_write_intent_rejects_an_unknown_action(tmp_path):
    with pytest.raises(ValueError):
        onboard.write_intent(state(tmp_path), "explode", "analytics", {}, "agent", 1)


@pytest.mark.parametrize("body", [
    "{not json",
    json.dumps({"action": "start", "project": "../escape", "payload": {}}),
    json.dumps({"action": "start", "project": "analytics", "payload": {"repos": []}}),
    json.dumps({"action": "start", "project": "analytics",
                "payload": {"repos": ["jesdi/analytics"], "board": {"owner": "jesdi"},
                            "commands": [{}]}}),
])
def test_a_malformed_intent_is_deleted_with_a_warning(tmp_path, box, body, capsys):
    d = state(tmp_path) / "onboarding-intents"
    d.mkdir(parents=True)
    (d / "1-analytics-start.json").write_text(body)
    run(box, FakeSh())
    assert list(d.iterdir()) == []
    assert record(tmp_path) is None
    assert "malformed onboarding intent" in capsys.readouterr().err


def test_a_second_start_leaves_the_record_alone(tmp_path, box):
    start(tmp_path)
    run(box, FakeSh())
    start(tmp_path, repos=["jesdi/other"], epoch_ms=2000)
    run(box, FakeSh())
    assert record(tmp_path)["request"]["repos"] == ["jesdi/analytics"]


# -- resolve ------------------------------------------------------------------


def test_resolve_builds_the_proposal_from_github(tmp_path, box):
    start(tmp_path, repos=["https://github.com/JESDI/Analytics.git"],
          commands=[{"setup_cmd": "make setup", "gate_cmd": "make gate",
                     "verify_cmd": "make e2e SLOT={slot}"}])
    sh = FakeSh({"gh api repos/JESDI/Analytics": gh_repo(202, "jesdi/analytics")})
    run(box, sh)
    rec = record(tmp_path)
    assert step(rec, "resolve")["state"] == "verified"
    home = Path.home()
    assert rec["proposal"] == {
        "name": "analytics", "paused": True,
        "board": {"owner": "jesdi", "number": 4},
        "repositories": [{
            "name": "analytics", "repo": "jesdi/analytics", "github_id": 202,
            "clone_path": str(home / "repos" / "analytics" / "analytics"),
            "worktrees_path": str(home / "repos" / "analytics" / "analytics.worktrees"),
            "setup_cmd": "make setup", "gate_cmd": "make gate",
            "verify_cmd": "make e2e SLOT={slot}"}]}
    assert len(rec["revision"]) == 64


@pytest.mark.parametrize("spelling", [
    "git@github.com:jesdi/analytics.git", "ssh://git@github.com/jesdi/analytics",
    "jesdi/analytics/", "https://github.com/jesdi/analytics"])
def test_resolve_accepts_every_spelling_of_a_repository(tmp_path, box, spelling):
    start(tmp_path, repos=[spelling])
    run(box, FakeSh())
    assert step(record(tmp_path), "resolve")["state"] == "verified"


def test_resolve_checks_the_board_with_the_project_token(tmp_path, box, monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "project-tok")
    start(tmp_path)
    sh = FakeSh()
    run(box, sh)
    [(_, _, env)] = sh.ran("graphql")
    assert env["GH_TOKEN"] == "project-tok"


def test_resolve_blocks_on_a_bad_slug(tmp_path, box):
    start(tmp_path, repos=["not a repo"])
    run(box, FakeSh())
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "'not a repo'" in s["detail"]


def test_resolve_blocks_on_a_missing_repository(tmp_path, box):
    start(tmp_path)
    run(box, FakeSh({"gh api repos/jesdi/analytics": failure("HTTP 404: Not Found")}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "jesdi/analytics" in s["detail"]


def test_resolve_fails_on_another_github_error_and_retries(tmp_path, box):
    start(tmp_path)
    run(box, FakeSh({"gh api repos/jesdi/analytics": failure("HTTP 502: Bad Gateway")}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "failed" and "502" in s["detail"]
    run(box, FakeSh())
    assert step(record(tmp_path), "resolve")["state"] == "verified"


def test_resolve_blocks_on_a_missing_board(tmp_path, box):
    start(tmp_path)
    run(box, FakeSh({"gh api graphql": NO_BOARD}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "jesdi/4" in s["detail"]


def test_resolve_blocks_a_repository_a_config_project_holds(tmp_path, box):
    """R1: the id decides, whatever the name GitHub gives now."""
    start(tmp_path, repos=["jesdi/renamed"])
    run(box, FakeSh({"gh api repos/jesdi/renamed": gh_repo(101, "jesdi/renamed")}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "'portfolio-eval'" in s["detail"]


def test_resolve_blocks_a_repository_another_open_record_holds(tmp_path, box):
    start(tmp_path, project="first")
    run(box, FakeSh())
    start(tmp_path, project="second", epoch_ms=2000)
    run(box, FakeSh())
    s = step(record(tmp_path, "second"), "resolve")
    assert s["state"] == "blocked" and "'first'" in s["detail"]


def test_resolve_blocks_a_proposal_the_live_config_refuses(tmp_path, box):
    start(tmp_path, project="portfolio-eval")
    run(box, FakeSh())
    s = step(record(tmp_path, "portfolio-eval"), "resolve")
    assert s["state"] == "blocked" and "used by two projects" in s["detail"]


def test_a_step_keeps_its_since_while_its_state_holds(tmp_path, box):
    start(tmp_path, repos=["bad slug"])
    run(box, FakeSh())
    later = NOW.replace(hour=13)
    run(box, FakeSh(), now=later)
    s = step(record(tmp_path), "resolve")
    assert s["since"] == NOW.isoformat()
    assert record(tmp_path)["updated_at"] == later.isoformat()


def test_a_run_with_nothing_open_calls_nothing(tmp_path, box):
    run(box, FakeSh({"": AssertionError("no call expected")}))


# -- clone --------------------------------------------------------------------


def clone_path(project="analytics", name="analytics") -> Path:
    return Path.home() / "repos" / project / name


def test_clone_clones_into_a_partial_path_then_renames_it(tmp_path, box):
    start(tmp_path)
    sh = FakeSh()
    run(box, sh)
    rec = record(tmp_path)
    assert step(rec, "clone")["state"] == "verified"
    [(argv, _, _)] = sh.ran("gh repo clone")
    assert argv == ["gh", "repo", "clone", "jesdi/analytics", f"{clone_path()}.partial"]
    assert (clone_path() / ".git").is_dir()
    assert not Path(f"{clone_path()}.partial").exists()
    assert rec["resources"] == [{"kind": "clone", "ref": str(clone_path()),
                                 "state": "in use"}]


def test_clone_keeps_a_clone_already_at_the_path(tmp_path, box):
    clone_path().mkdir(parents=True)
    start(tmp_path)
    sh = FakeSh({"remote get-url origin": "git@github.com:JESDI/analytics.git\n"})
    run(box, sh)
    assert step(record(tmp_path), "clone")["state"] == "verified"
    assert sh.ran("gh repo clone") == []


def test_clone_blocks_when_another_repository_is_at_the_path(tmp_path, box):
    clone_path().mkdir(parents=True)
    start(tmp_path)
    run(box, FakeSh({"remote get-url origin": "https://github.com/jesdi/other\n"}))
    s = step(record(tmp_path), "clone")
    assert s["state"] == "blocked"
    assert str(clone_path()) in s["detail"] and "jesdi/other" in s["detail"]


def test_clone_removes_a_stale_partial_clone_first(tmp_path, box):
    partial = Path(f"{clone_path()}.partial")
    (partial / "half").mkdir(parents=True)
    start(tmp_path)
    run(box, FakeSh())
    assert step(record(tmp_path), "clone")["state"] == "verified"
    assert not (clone_path() / "half").exists()


def test_a_failed_clone_is_retried_on_the_next_run(tmp_path, box):
    start(tmp_path)
    run(box, FakeSh({"gh repo clone": failure("fatal: early EOF")}))
    s = step(record(tmp_path), "clone")
    assert s["state"] == "failed" and "early EOF" in s["detail"]
    assert not clone_path().exists()
    sh = FakeSh()
    run(box, sh)
    assert step(record(tmp_path), "clone")["state"] == "verified"
    assert sh.ran("gh api repos/") == []  # resolve stays verified


# -- confirm ------------------------------------------------------------------


def intent(tmp_path, action, payload=None, project="analytics", epoch_ms=5000):
    return onboard.write_intent(state(tmp_path), action, project, payload or {},
                                actor="agent", epoch_ms=epoch_ms)


def resolved(tmp_path, box, **kw):
    start(tmp_path, **kw)
    run(box, FakeSh())
    return record(tmp_path)


def test_confirm_blocks_with_the_command_to_run(tmp_path, box):
    rec = resolved(tmp_path, box)
    s = step(rec, "confirm")
    assert s["state"] == "blocked"
    assert rec["revision"][:12] in s["detail"]
    assert "agent-ops project add analytics jesdi/analytics --board jesdi/4" in s["detail"]


def test_confirm_of_the_current_revision_verifies_the_step(tmp_path, box):
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["confirmed_revision"] == rec["revision"]
    assert step(rec, "confirm")["state"] == "verified"


def test_a_stale_confirm_is_refused_into_the_notice(tmp_path, box):
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": "0" * 64})
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["confirmed_revision"] == ""
    assert "000000000000" in rec["notice"] and rec["revision"][:12] in rec["notice"]
    assert step(rec, "confirm")["state"] == "blocked"


def test_an_edit_before_confirm_resolves_the_new_request(tmp_path, box):
    first = resolved(tmp_path, box)
    payload = {"repos": ["jesdi/analytics"], "board": {"owner": "jesdi", "number": 4},
               "commands": [{"gate_cmd": "make check"}]}
    intent(tmp_path, "edit", payload)
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["proposal"]["repositories"][0]["gate_cmd"] == "make check"
    assert rec["revision"] != first["revision"]
    assert step(rec, "confirm")["state"] == "blocked"
    # the revision is the proposal's digest: the same proposal, the same one
    intent(tmp_path, "edit", {**payload, "commands": [{"gate_cmd": "make gate"}]},
           epoch_ms=6000)
    run(box, FakeSh())
    assert record(tmp_path)["revision"] == first["revision"]


def test_an_edit_after_confirm_is_refused(tmp_path, box):
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    intent(tmp_path, "edit", {"repos": ["jesdi/web"], "board": {"owner": "jesdi", "number": 4},
                              "commands": [{"gate_cmd": "x"}]}, epoch_ms=6000)
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["request"]["repos"] == ["jesdi/analytics"]
    assert "after confirm" in rec["notice"]


@pytest.mark.parametrize("action", ["edit", "confirm", "resume"])
def test_an_intent_for_no_open_record_is_dropped(tmp_path, box, action, capsys):
    intent(tmp_path, action, {"revision": "x"})
    run(box, FakeSh())
    assert record(tmp_path) is None
    assert "no open onboarding" in capsys.readouterr().err


def test_resume_retries_the_current_step(tmp_path, box):
    start(tmp_path)
    run(box, FakeSh({"gh repo clone": failure("boom")}))
    intent(tmp_path, "resume")
    run(box, FakeSh())
    assert step(record(tmp_path), "clone")["state"] == "verified"
