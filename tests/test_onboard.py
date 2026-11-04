"""The onboarding engine (dispatcher.onboard): one run drains the onboarding
intents and moves every open record as far as it goes. Every subprocess goes
through the fake `sh`, every HTTP request through the fake `get`."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from dispatcher import onboard
from dispatcher.infra_config import add_project_text, project_block
from tests.fake_github import FakeGitHub

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
    return json.dumps({"id": github_id, "node_id": f"R_{github_id}", "full_name": full_name,
                       "name": full_name.split("/")[1]})


def failure(stderr):
    return subprocess.CalledProcessError(1, ["x"], output="", stderr=stderr)


def cloned(argv, cwd, env):
    """`git clone <https url> <path>`: the clone appears at <path>."""
    Path(argv[-1]).mkdir(parents=True)
    (Path(argv[-1]) / ".git").mkdir()
    return ""


ORIGIN = "https://github.com/jesdi/analytics.git\n"

# The checkout the engine executes from: the only place its own revision is read.
CHECKOUT = Path(onboard.__file__).resolve().parents[1]
BUILDER = "builder-b\n"


class FakeSh:
    """Answers each call from the first route whose key is a substring of
    the joined argv: a string, an exception to raise, or a callable taking
    (argv, cwd, env). An unrouted call fails the test."""

    def __init__(self, routes=None):
        self.github = FakeGitHub()      # board jesdi/4, complete and empty
        self.github.board()
        self.routes = {"gh api repos/jesdi/analytics": gh_repo(202, "jesdi/analytics"),
                       "gh api graphql": self.github, "gh label": self.github,
                       "git clone": cloned,
                       "remote get-url origin": ORIGIN, **(routes or {})}
        self.calls = []
        self.builder = BUILDER  # the executing checkout's HEAD: text, or an exception

    def route(self, key, answer):
        self.routes = {key: answer, **{k: v for k, v in self.routes.items() if k != key}}

    def __call__(self, argv, cwd=None, env=None):
        self.calls.append((argv, cwd, env))
        line = " ".join(argv)
        if "rev-parse" in argv and (str(CHECKOUT) in line or cwd and str(cwd) == str(CHECKOUT)):
            if isinstance(self.builder, Exception):
                raise self.builder
            return self.builder
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
def box(tmp_path, monkeypatch):
    """The live config and the state dir the conftest points the loader at.
    ~ expands to the conftest's home."""
    monkeypatch.setenv("HOME", str(Path.home()))
    p = tmp_path / "targets.yaml"
    p.write_text(LIVE)
    return p


def state(tmp_path) -> Path:
    return tmp_path / "state"


def add(tmp_path, project="analytics", repos=("jesdi/analytics",),
          board=None, commands=None, epoch_ms=1000):
    return onboard.write_intent(state(tmp_path), "add", project, {
        "repos": list(repos), "board": board or {"owner": "jesdi", "number": 4},
        "commands": commands or [{"setup_cmd": "", "gate_cmd": "make gate",
                                  "verify_cmd": ""} for _ in repos]},
        actor="agent", epoch_ms=epoch_ms)


def run(box, sh, get=no_get, now=NOW):
    onboard.run(box, sh=sh, get=get, now=now)


def record(tmp_path, project="analytics"):
    """The record file, as JSON: its layout is the engine's own."""
    p = state(tmp_path) / "onboarding" / f"{project}.json"
    return json.loads(p.read_text()) if p.exists() else None


def step(rec, name):
    return next(s for s in rec["steps"] if s["name"] == name)


# -- intents ------------------------------------------------------------------


def test_add_intent_opens_a_record_and_is_deleted(tmp_path, box):
    p = add(tmp_path)
    assert p.parent == state(tmp_path) / "onboarding-intents"
    assert p.name == "1000-analytics-add.json"
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
    json.dumps({"action": "add", "project": "../escape", "payload": {}}),
    json.dumps({"action": "add", "project": "analytics", "payload": {"repos": []}}),
    json.dumps({"action": "add", "project": "analytics",
                "payload": {"repos": ["jesdi/analytics"], "board": {"owner": "jesdi"},
                            "commands": [{}]}}),
])
def test_a_malformed_intent_is_deleted_with_a_warning(tmp_path, box, body, capsys):
    d = state(tmp_path) / "onboarding-intents"
    d.mkdir(parents=True)
    (d / "1-analytics-add.json").write_text(body)
    run(box, FakeSh())
    assert list(d.iterdir()) == []
    assert record(tmp_path) is None
    assert "malformed intent" in capsys.readouterr().err


# -- resolve ------------------------------------------------------------------


def test_resolve_builds_the_proposal_from_github(tmp_path, box):
    add(tmp_path, repos=["https://github.com/JESDI/Analytics.git"],
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
    add(tmp_path, repos=[spelling])
    run(box, FakeSh())
    assert step(record(tmp_path), "resolve")["state"] == "verified"


def test_resolve_checks_the_board_with_the_project_token(tmp_path, box, monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "project-tok")
    add(tmp_path)
    sh = FakeSh()
    run(box, sh)
    board_calls = [env for argv, _, env in sh.ran("graphql") if "ProjectV2" in " ".join(argv)]
    issue_calls = [env for argv, _, env in sh.ran("graphql") if "issues(first" in " ".join(argv)]
    assert board_calls and all(env["GH_TOKEN"] == "project-tok" for env in board_calls)
    # issues are the repository's: read with the box's stored auth, not the board token
    assert issue_calls and all(env is None for env in issue_calls)


def test_resolve_blocks_on_a_bad_slug(tmp_path, box):
    add(tmp_path, repos=["not a repo"])
    run(box, FakeSh())
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "'not a repo'" in s["detail"]


def test_resolve_blocks_on_a_missing_repository(tmp_path, box):
    add(tmp_path)
    run(box, FakeSh({"gh api repos/jesdi/analytics": failure("HTTP 404: Not Found")}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "jesdi/analytics" in s["detail"]


def test_resolve_fails_on_another_github_error_and_retries(tmp_path, box):
    add(tmp_path)
    run(box, FakeSh({"gh api repos/jesdi/analytics": failure("HTTP 502: Bad Gateway")}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "failed" and "502" in s["detail"]
    run(box, FakeSh())
    assert step(record(tmp_path), "resolve")["state"] == "verified"


def test_resolve_blocks_on_a_missing_board(tmp_path, box):
    add(tmp_path)
    run(box, FakeSh({"gh api graphql": NO_BOARD}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "jesdi/4" in s["detail"]


def test_resolve_blocks_a_repository_a_config_project_holds(tmp_path, box):
    """R1: the id decides, whatever the name GitHub gives now."""
    add(tmp_path, repos=["jesdi/renamed"])
    run(box, FakeSh({"gh api repos/jesdi/renamed": gh_repo(101, "jesdi/renamed")}))
    s = step(record(tmp_path), "resolve")
    assert s["state"] == "blocked" and "'portfolio-eval'" in s["detail"]


def test_resolve_blocks_a_repository_another_open_record_holds(tmp_path, box):
    add(tmp_path, project="first")
    run(box, FakeSh())
    add(tmp_path, project="second", epoch_ms=2000)
    run(box, FakeSh())
    s = step(record(tmp_path, "second"), "resolve")
    assert s["state"] == "blocked" and "'first'" in s["detail"]


def test_resolve_blocks_a_proposal_the_live_config_refuses(tmp_path, box):
    add(tmp_path, project="portfolio-eval")
    run(box, FakeSh())
    s = step(record(tmp_path, "portfolio-eval"), "resolve")
    assert s["state"] == "blocked" and "used by two projects" in s["detail"]


def test_a_step_keeps_its_since_while_its_state_holds(tmp_path, box):
    add(tmp_path, repos=["bad slug"])
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
    add(tmp_path)
    sh = FakeSh()
    run(box, sh)
    rec = record(tmp_path)
    assert step(rec, "clone")["state"] == "verified"
    [(argv, _, _)] = sh.ran("git clone")
    assert argv == ["git", "clone", "https://github.com/jesdi/analytics.git",
                    f"{clone_path()}.partial"]
    assert (clone_path() / ".git").is_dir()
    assert not Path(f"{clone_path()}.partial").exists()
    assert rec["resources"] == [{"kind": "clone", "ref": str(clone_path()),
                                 "state": "in use"}]


def test_clone_keeps_a_clone_already_at_the_path(tmp_path, box):
    clone_path().mkdir(parents=True)
    add(tmp_path)
    sh = FakeSh({"remote get-url origin": "git@github.com:JESDI/analytics.git\n"})
    run(box, sh)
    assert step(record(tmp_path), "clone")["state"] == "verified"
    assert sh.ran("git clone") == []


def test_clone_blocks_when_another_repository_is_at_the_path(tmp_path, box):
    clone_path().mkdir(parents=True)
    add(tmp_path)
    run(box, FakeSh({"remote get-url origin": "https://github.com/jesdi/other\n"}))
    s = step(record(tmp_path), "clone")
    assert s["state"] == "blocked"
    assert str(clone_path()) in s["detail"] and "jesdi/other" in s["detail"]


@pytest.mark.parametrize("leftover", ["", ".git"])
def test_clone_removes_an_empty_or_partial_clone_first(tmp_path, box, leftover):
    partial = Path(f"{clone_path()}.partial")
    (partial / leftover).mkdir(parents=True)
    add(tmp_path)
    run(box, FakeSh())
    assert step(record(tmp_path), "clone")["state"] == "verified"
    assert not partial.exists()


@pytest.mark.parametrize("leftover,origin", [
    ("notes", ORIGIN), (".git", "https://github.com/jesdi/other\n"),
    (".git", failure("error: No such remote 'origin'"))])
def test_clone_blocks_on_a_partial_path_holding_something_else(
        tmp_path, box, leftover, origin):
    partial = Path(f"{clone_path()}.partial")
    (partial / leftover).mkdir(parents=True)
    add(tmp_path)
    sh = FakeSh({"remote get-url origin": origin})
    run(box, sh)
    s = step(record(tmp_path), "clone")
    assert s["state"] == "blocked" and str(partial) in s["detail"]
    assert (partial / leftover).exists() and sh.ran("git clone") == []


def test_a_failed_clone_is_retried_on_the_next_run(tmp_path, box):
    add(tmp_path)
    run(box, FakeSh({"git clone": failure("fatal: early EOF")}))
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
    add(tmp_path, **kw)
    run(box, FakeSh())
    return record(tmp_path)


def test_confirm_blocks_with_the_command_to_run(tmp_path, box):
    rec = resolved(tmp_path, box)
    s = step(rec, "confirm")
    assert s["state"] == "blocked"
    assert rec["revision"][:12] in s["detail"]
    assert "agent-ops project add analytics jesdi/analytics --board jesdi/4" in s["detail"]
    assert rec["awaiting"] == "confirm"


def test_confirm_of_the_current_revision_verifies_the_step(tmp_path, box):
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["confirmed_revision"] == rec["revision"]
    assert step(rec, "confirm")["state"] == "verified"
    assert rec["awaiting"] == ""


def test_a_stale_confirm_is_refused_into_the_notice(tmp_path, box):
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": "0" * 64})
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["confirmed_revision"] == ""
    assert "000000000000" in rec["notice"] and rec["revision"][:12] in rec["notice"]
    assert step(rec, "confirm")["state"] == "blocked"


def test_the_same_request_again_resumes(tmp_path, box):
    """Any spelling of the same repositories is the same request; every run
    retries the current step."""
    add(tmp_path)
    run(box, FakeSh({"git clone": failure("boom")}))
    add(tmp_path, repos=["https://github.com/JESDI/analytics.git"], epoch_ms=2000)
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["request"]["repos"] == ["jesdi/analytics"] and rec["notice"] == ""
    assert step(rec, "clone")["state"] == "verified"


def test_another_request_before_confirm_is_resolved_again(tmp_path, box):
    first = resolved(tmp_path, box)
    sh = FakeSh({"gh api repos/jesdi/web": gh_repo(303, "jesdi/web")})
    add(tmp_path, repos=["jesdi/analytics", "jesdi/web"], epoch_ms=2000)
    run(box, sh)
    rec = record(tmp_path)
    assert [x["repo"] for x in rec["proposal"]["repositories"]] == [
        "jesdi/analytics", "jesdi/web"]
    assert rec["revision"] != first["revision"]
    # the revision is the proposal's digest: the same proposal, the same one
    add(tmp_path, epoch_ms=3000)
    run(box, FakeSh())
    assert record(tmp_path)["revision"] == first["revision"]


def test_other_repositories_after_confirm_are_refused(tmp_path, box):
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    add(tmp_path, repos=["jesdi/web"], epoch_ms=6000)
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["request"]["repos"] == ["jesdi/analytics"]
    assert "refused" in rec["notice"] and "after confirm" in rec["notice"]
    assert step(rec, "confirm")["state"] == "verified"


def test_new_commands_after_confirm_go_back_to_confirm_and_update_the_pr(
        tmp_path, box, infra_token):
    """Before the merge, a changed board or command is a new revision: the
    record goes back to confirm, then config-pr pushes it onto the open PR."""
    first = confirmed(tmp_path, box, FakeSh(infra_routes(**NO_CI_RUN)))
    assert step(first, "config-merge")["state"] == "waiting"
    add(tmp_path, commands=[{"gate_cmd": "make check"}], epoch_ms=6000)
    run(box, FakeSh())
    rec = record(tmp_path)
    assert rec["notice"] == "" and rec["awaiting"] == "confirm"
    assert rec["revision"] != first["revision"]
    assert [s["state"] for s in rec["steps"] if s["name"] not in ("resolve", "clone", "inspect")] == ["blocked"] + ["pending"] * 5
    intent(tmp_path, "confirm", {"revision": rec["revision"]}, epoch_ms=7000)
    sh = FakeSh(infra_routes(open_prs=json.dumps(
        [{"number": 7, "url": PR_URL, "headRefOid": "head1"}]), **NO_CI_RUN))
    sh.route("rev-parse HEAD", "head2\n")
    sh.route("for-each-ref", "head1\n")
    run(box, sh)
    rec = record(tmp_path)
    assert (rec["infra_pr"]["head"], rec["infra_pr"]["revision"]) == ("head2", rec["revision"])
    assert len(sh.ran(" push ")) == 1 and sh.ran("gh pr create") == []


def test_a_changed_request_after_the_merge_is_refused(tmp_path, box, infra_token):
    merged(tmp_path, box)
    add(tmp_path, commands=[{"gate_cmd": "make check"}], epoch_ms=6000)
    run(box, FakeSh(infra_routes()))
    rec = record(tmp_path)
    assert "edit targets.yaml by hand for now" in rec["notice"]
    assert rec["request"]["commands"][0]["gate_cmd"] == "make gate"


def test_the_same_request_for_a_complete_onboarding_says_so(tmp_path, box, infra_token):
    deployed(tmp_path, box, FakeGet(LISTED))
    add(tmp_path, epoch_ms=9000)
    run(box, FakeSh({"": AssertionError("no call expected")}))
    rec = record(tmp_path)
    assert rec["status"] == "complete" and rec["notice"] == "already onboarded"


def test_a_confirm_for_no_open_record_is_dropped(tmp_path, box, capsys):
    intent(tmp_path, "confirm", {"revision": "x"})
    run(box, FakeSh())
    assert record(tmp_path) is None
    assert "no open onboarding" in capsys.readouterr().err


# -- config-pr ----------------------------------------------------------------

PR_URL = "https://github.com/jesdi/agent-ops-infra/pull/7"


def infra_cloned(argv, cwd, env):
    cloned(argv, cwd, env)
    (Path(argv[-1]) / "config").mkdir()
    return ""


def checks(*rows):
    """The workflow runs on the head: ci green, plus each (name, status,
    conclusion) as another workflow's run."""
    return json.dumps({"workflow_runs": [
        {"id": 1, "name": "ci", "status": "completed", "conclusion": "success",
         "html_url": "https://github.com/jesdi/agent-ops-infra/actions/runs/1"},
        *({"id": i, "name": name, "status": status, "conclusion": conclusion,
           "html_url": f"https://github.com/jesdi/agent-ops-infra/runs/{name}"}
          for i, (name, status, conclusion) in enumerate(rows, start=2))]})


def updater(result, ended):
    return f"Result={result}\nExecMainExitTimestamp=@{int(ended.timestamp())}\n"


def ci_run(status="completed", conclusion="success"):
    return json.dumps({"workflow_runs": [{
        "id": 1, "name": "ci", "status": status, "conclusion": conclusion,
        "html_url": "https://github.com/jesdi/agent-ops-infra/actions/runs/1"}]})


def statuses(*rows):
    return json.dumps({"statuses": [
        {"context": context, "state": st, "target_url": f"https://ci.example/{context}"}
        for context, st in rows]})


CI_RUN = ci_run()


def pr_view(state, base="main", oid="merge1", at="2026-11-02T12:00:00Z"):
    merged = state == "MERGED"
    return json.dumps({"state": state, "baseRefName": base,
                       "mergeCommit": {"oid": oid} if merged else None,
                       "mergedAt": at if merged else None})


MERGED = {"gh pr view 7": pr_view("MERGED")}


def infra_routes(main=LIVE, open_prs="[]", **merge):
    """agent-ops-infra as the scratch clone and gh see it: by default the PR
    is open on main and its ci run green."""
    return {**{"gh pr view 7": pr_view("OPEN"),
               "actions/runs?head_sha=head1": CI_RUN,
               "commits/head1/status": statuses(),
               "merge-base --is-ancestor merge1 HEAD": failure(""),
               "systemctl --user show agent-ops-update.service": updater("success", NOW)},
            **merge,
            "clone https://github.com/jesdi/agent-ops-infra.git": infra_cloned,
            "fetch origin": "",
            "show origin/main:config/targets.yaml": main,
            "gh pr list --repo jesdi/agent-ops-infra": open_prs,
            "checkout -B onboard/analytics origin/main": "",
            "add config/targets.yaml": "", "commit -m": "",
            "rev-parse HEAD": "head1\n", "rev-parse origin/main": "base1\n",
            "for-each-ref": "",  # no onboard/analytics on infra yet
            "show -s --format=%T HEAD": "tree1\n",  # the commit built here
            # a head someone else pushed: today's main, another tree
            "show -s --format=%P %T": "base1 theirtree\n",
            " push ": "",
            "gh pr create": PR_URL + "\n"}


@pytest.fixture
def infra_token(monkeypatch):
    monkeypatch.setenv("GH_INFRA_TOKEN", "infra-tok")


def confirmed(tmp_path, box, sh=None):
    """A record confirmed, then one run with `sh`."""
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    sh = sh or FakeSh(infra_routes())
    run(box, sh)
    return record(tmp_path)


def scratch(tmp_path) -> Path:
    return state(tmp_path) / "onboarding" / "infra"


NO_CI_RUN = {"actions/runs?head_sha=head1": json.dumps({"workflow_runs": []})}


def test_config_pr_pushes_the_project_and_opens_the_pr(tmp_path, box, infra_token):
    sh = FakeSh(infra_routes(**NO_CI_RUN))
    rec = confirmed(tmp_path, box, sh)
    s = step(rec, "config-pr")
    assert (s["state"], s["link"]) == ("verified", PR_URL)
    assert rec["infra_pr"] == {
        "number": 7, "url": PR_URL, "head": "head1", "base": "base1",
        "revision": rec["revision"], "builder": "builder-b", "pushed_at": NOW.isoformat(),
        }
    assert (rec["merge_sha"], rec["merged_at"]) == ("", "")
    assert {"kind": "infra-pr", "ref": PR_URL, "state": "in use"} in rec["resources"]
    written = (scratch(tmp_path) / "config" / "targets.yaml").read_text()
    assert written.startswith(LIVE) and "  - name: analytics\n" in written
    [(argv, _, env)] = sh.ran(" push ")
    assert argv[-3:] == ["push", "origin", "HEAD:refs/heads/onboard/analytics"]
    assert "credential.helper=!gh auth git-credential" in argv
    assert env["GH_TOKEN"] == "infra-tok"
    for key in ("clone https://github.com/jesdi/agent-ops-infra", "fetch", "gh pr list",
                "gh pr create"):
        assert all(env["GH_TOKEN"] == "infra-tok" for _, _, env in sh.ran(key)), key


def test_config_pr_blocks_without_the_infra_token(tmp_path, box, monkeypatch):
    monkeypatch.delenv("GH_INFRA_TOKEN", raising=False)
    s = step(confirmed(tmp_path, box), "config-pr")
    assert s["state"] == "blocked" and "GH_INFRA_TOKEN" in s["detail"]


def main_holding(tmp_path, *changes):
    """infra main's targets.yaml holding the record's proposal: as it is,
    or with the n-th call's `changes[n]` applied to its gate_cmd."""
    calls = []

    def show(argv, cwd, env):
        proposal = record(tmp_path)["proposal"]
        if len(calls) < len(changes) and changes[len(calls)]:
            proposal["repositories"][0]["gate_cmd"] = changes[len(calls)]
        calls.append(argv)
        return add_project_text(LIVE, project_block(proposal))
    return show


def test_config_pr_is_verified_when_infra_main_has_the_proposal(tmp_path, box, infra_token):
    sh = FakeSh(infra_routes(main=main_holding(tmp_path)))
    s = step(confirmed(tmp_path, box, sh), "config-pr")
    assert s["state"] == "verified"
    assert sh.ran(" push ") == []


def test_config_pr_blocks_on_another_entry_of_the_project_on_infra_main(
        tmp_path, box, infra_token):
    sh = FakeSh(infra_routes(main=main_holding(tmp_path, "rm -rf /")))
    rec = confirmed(tmp_path, box, sh)
    s = step(rec, "config-pr")
    assert s["state"] == "blocked" and "infra main has a different analytics entry" in s["detail"]
    assert sh.ran(" push ") == [] and rec["merge_sha"] == ""


def test_config_merge_blocks_when_the_main_entry_changed_since_config_pr(
        tmp_path, box, infra_token):
    sh = FakeSh(infra_routes(main=main_holding(tmp_path, "", "rm -rf /"), **{
        "log -1 --format": "add1 2026-11-02T11:58:00Z\n"}))
    rec = confirmed(tmp_path, box, sh)
    s = step(rec, "config-merge")
    assert s["state"] == "blocked" and "infra main has a different analytics entry" in s["detail"]
    assert rec["merge_sha"] == ""


def test_config_merge_blocks_when_the_entry_left_main_since_config_pr(
        tmp_path, box, infra_token):
    holding = main_holding(tmp_path)
    answers = iter([holding, lambda *_: LIVE])
    sh = FakeSh(infra_routes(main=lambda *a: next(answers)(*a)))
    s = step(confirmed(tmp_path, box, sh), "config-merge")
    assert s["state"] == "blocked" and "no longer lists analytics" in s["detail"]


def test_a_project_already_on_infra_main_runs_through_config_merge_to_deploy(
        tmp_path, box, infra_token):
    """No PR to merge: the infra main commit that added the project is the
    merge, and deploy waits for the box to hold it."""
    sh = FakeSh(infra_routes(main=main_holding(tmp_path), **{
        "log -1 --format=%H %cI -S - name: analytics origin/main -- config/targets.yaml":
            "add1 2026-11-02T11:58:00Z\n",
        "merge-base --is-ancestor add1 HEAD": failure("")}))
    rec = confirmed(tmp_path, box, sh)
    assert [step(rec, n)["state"] for n in ("config-pr", "config-merge", "deploy")] == [
        "verified", "verified", "waiting"]
    assert rec["infra_pr"] is None
    assert (rec["merge_sha"], rec["merged_at"]) == ("add1", "2026-11-02T11:58:00+00:00")
    assert sh.ran("gh pr") == [] and sh.ran(" push ") == []
    box.write_text(DEPLOYED)
    run(box, FakeSh(infra_routes(**{"merge-base --is-ancestor add1 HEAD": ""})))
    assert step(record(tmp_path), "deploy")["state"] == "verified"


def test_config_merge_blocks_when_no_main_commit_adds_the_entry(tmp_path, box, infra_token):
    sh = FakeSh(infra_routes(main=main_holding(tmp_path), **{"log -1 --format": ""}))
    s = step(confirmed(tmp_path, box, sh), "config-merge")
    assert s["state"] == "blocked" and "no commit there adds" in s["detail"]


def test_config_pr_blocks_on_a_branch_head_it_did_not_push(tmp_path, box, infra_token):
    """onboard/<p> holds a commit the record doesn't name: someone else's
    work, so no force-push over it."""
    sh = FakeSh(infra_routes(open_prs=json.dumps(
        [{"number": 7, "url": PR_URL, "headRefOid": "theirs1"}])))
    sh.route("for-each-ref", "theirs1\n")
    rec = confirmed(tmp_path, box, sh)
    s = step(rec, "config-pr")
    assert s["state"] == "blocked" and "theirs1" in s["detail"] and s["link"] == PR_URL
    assert "until `agent-ops project abandon` ships" in s["detail"]
    assert sh.ran(" push ") == [] and sh.ran("gh pr create") == []
    assert {argv[-1] for argv, _, _ in sh.ran("for-each-ref")} == {
        "refs/remotes/origin/onboard/analytics"}
    assert all("--prune" in argv for argv, _, _ in sh.ran("fetch"))


def test_config_pr_keeps_an_open_pr_that_matches_the_record(tmp_path, box, infra_token):
    rec = confirmed(tmp_path, box)
    step(rec, "config-pr")["state"] = "failed"  # a crash after the push
    onboard.write_json_atomic(state(tmp_path) / "onboarding" / "analytics.json", rec)
    sh = FakeSh(infra_routes(open_prs=json.dumps(
        [{"number": 7, "url": PR_URL, "headRefOid": "head1"}])))
    run(box, sh)
    assert step(record(tmp_path), "config-pr")["state"] == "verified"
    assert sh.ran(" push ") == []


def test_config_pr_rebuilds_an_open_pr_on_a_main_that_moved(tmp_path, box, infra_token):
    rec = confirmed(tmp_path, box)
    step(rec, "config-pr")["state"] = "failed"
    onboard.write_json_atomic(state(tmp_path) / "onboarding" / "analytics.json", rec)
    sh = FakeSh(infra_routes(open_prs=json.dumps(
        [{"number": 7, "url": PR_URL, "headRefOid": "head1"}])))
    sh.route("rev-parse origin/main", "base2\n")
    sh.route("for-each-ref", "head1\n")
    run(box, sh)
    rec = record(tmp_path)
    assert step(rec, "config-pr")["state"] == "verified"
    assert rec["infra_pr"]["base"] == "base2" and len(sh.ran(" push ")) == 1


def test_config_pr_blocks_when_infra_config_refuses_the_project(tmp_path, box, infra_token):
    taken = LIVE.replace("github_id: 101", "github_id: 202")
    s = step(confirmed(tmp_path, box, FakeSh(infra_routes(main=taken))), "config-pr")
    assert s["state"] == "blocked" and "github_id" in s["detail"]


# -- config-merge -------------------------------------------------------------


def test_config_merge_blocks_on_a_green_pr_for_the_operator_to_merge(
        tmp_path, box, infra_token):
    """Onboarding has no merge rights on infra main, the box's deploy source."""
    sh = FakeSh(infra_routes())
    rec = confirmed(tmp_path, box, sh)
    s = step(rec, "config-merge")
    assert (s["state"], s["link"]) == ("blocked", PR_URL)
    assert "infra PR is green: merge it" in s["detail"]
    assert (rec["merge_sha"], rec["merged_at"]) == ("", "")
    assert sh.ran("/merge") == [] and sh.ran("-X PUT") == []
    assert step(rec, "deploy")["state"] == "pending"


def test_config_merge_is_verified_once_the_operator_merged_the_pr(tmp_path, box, infra_token):
    """Any merge method: the merge commit GitHub names is the deploy's."""
    confirmed(tmp_path, box)
    sh = FakeSh(infra_routes(**{"gh pr view 7": pr_view(
        "MERGED", oid="m9", at="2026-11-02T12:05:00Z"),
        "merge-base --is-ancestor m9 HEAD": failure("")}))
    run(box, sh, now=NOW + timedelta(minutes=6))
    rec = record(tmp_path)
    s = step(rec, "config-merge")
    assert (s["state"], s["link"]) == ("verified", PR_URL)
    assert (rec["merge_sha"], rec["merged_at"]) == ("m9", "2026-11-02T12:05:00+00:00")
    assert step(rec, "deploy")["state"] == "waiting"
    assert sh.ran("/merge") == []


def test_config_merge_waits_for_a_ci_run_then_blocks_after_30_minutes(
        tmp_path, box, infra_token):
    no_run = NO_CI_RUN
    s = step(confirmed(tmp_path, box, FakeSh(infra_routes(**no_run))), "config-merge")
    assert s["state"] == "waiting" and "no ci run" in s["detail"]
    run(box, FakeSh(infra_routes(**no_run)), now=NOW + timedelta(minutes=29))
    assert step(record(tmp_path), "config-merge")["state"] == "waiting"
    run(box, FakeSh(infra_routes(**no_run)), now=NOW + timedelta(minutes=31))
    s = step(record(tmp_path), "config-merge")
    assert s["state"] == "blocked" and "no ci run" in s["detail"] and s["link"] == PR_URL


def test_the_infra_commit_uses_the_box_git_identity(tmp_path, box, infra_token):
    """The box signs with a key registered to its git identity; a made-up
    author has no GitHub user, so the signature shows Unverified."""
    sh = FakeSh(infra_routes())
    confirmed(tmp_path, box, sh)
    [(argv, _, _)] = sh.ran("commit -m")
    assert not any(a.startswith(("user.name", "user.email")) for a in argv)


def test_a_skipped_workflow_besides_ci_does_not_hold_the_merge(tmp_path, box, infra_token):
    skipped = {"actions/runs?head_sha=head1": checks(("docs", "completed", "skipped"))}
    s = step(confirmed(tmp_path, box, FakeSh(infra_routes(**skipped))), "config-merge")
    assert s["state"] == "blocked" and "green" in s["detail"]


def test_config_merge_waits_while_the_ci_run_is_queued_without_checks(
        tmp_path, box, infra_token):
    queued = {"actions/runs?head_sha=head1": ci_run("queued", None)}
    sh = FakeSh(infra_routes(**queued))
    s = step(confirmed(tmp_path, box, sh), "config-merge")
    assert s["state"] == "waiting" and "ci" in s["detail"]
    assert sh.ran("/merge") == []


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "cancelled"])
def test_config_merge_wants_the_ci_run_to_succeed(tmp_path, box, infra_token, conclusion):
    sh = FakeSh(infra_routes(**{"actions/runs?head_sha=head1": ci_run(conclusion=conclusion)}))
    assert step(confirmed(tmp_path, box, sh), "config-merge")["state"] == "waiting"
    assert sh.ran("/merge") == []


def test_config_merge_waits_on_a_pending_commit_status_and_blocks_on_a_red_one(
        tmp_path, box, infra_token):
    pending = {"commits/head1/status": statuses(("deploy-preview", "pending"))}
    sh = FakeSh(infra_routes(**pending))
    s = step(confirmed(tmp_path, box, sh), "config-merge")
    assert s["state"] == "waiting" and "deploy-preview" in s["detail"]
    sh = FakeSh(infra_routes(**{"commits/head1/status": statuses(("deploy-preview", "error"))}))
    run(box, sh)
    s = step(record(tmp_path), "config-merge")
    assert s["state"] == "blocked" and "deploy-preview is error" in s["detail"]
    assert s["link"] == "https://ci.example/deploy-preview"
    assert sh.ran("/merge") == []


def test_config_merge_names_the_checks_still_pending(tmp_path, box, infra_token):
    pending = {"actions/runs?head_sha=head1": checks(("test", "in_progress", None),
                                                  ("lint", "completed", "success"))}
    s = step(confirmed(tmp_path, box, FakeSh(infra_routes(**pending))), "config-merge")
    assert s["state"] == "waiting" and "test" in s["detail"] and "lint" not in s["detail"]
    sh = FakeSh(infra_routes(**pending))
    run(box, sh, now=NOW + timedelta(minutes=31))
    s = step(record(tmp_path), "config-merge")
    assert s["state"] == "blocked" and "test" in s["detail"]
    assert sh.ran("/merge") == []


def test_config_merge_blocks_on_a_red_check_with_its_link(tmp_path, box, infra_token):
    red = {"actions/runs?head_sha=head1": checks(("test", "completed", "failure"),
                                              ("lint", "in_progress", None))}
    sh = FakeSh(infra_routes(**red))
    s = step(confirmed(tmp_path, box, sh), "config-merge")
    assert s["state"] == "blocked" and "test" in s["detail"] and "failure" in s["detail"]
    assert s["link"] == "https://github.com/jesdi/agent-ops-infra/runs/test"
    assert sh.ran("/merge") == []


def test_config_merge_rebuilds_the_pr_when_main_moved(tmp_path, box, infra_token):
    no_run = NO_CI_RUN
    confirmed(tmp_path, box, FakeSh(infra_routes(**no_run)))
    sh = FakeSh(infra_routes(**no_run))
    sh.route("rev-parse origin/main", "base2\n")
    sh.route("rev-parse HEAD", "head2\n")
    sh.route("for-each-ref", "head1\n")
    run(box, sh, now=NOW + timedelta(minutes=40))
    rec = record(tmp_path)
    s = step(rec, "config-merge")
    assert s["state"] == "waiting" and "main moved" in s["detail"]
    assert (rec["infra_pr"]["head"], rec["infra_pr"]["base"]) == ("head2", "base2")
    assert rec["infra_pr"]["pushed_at"] == (NOW + timedelta(minutes=40)).isoformat()
    [(argv, _, _)] = sh.ran(" push ")
    assert argv[-4:] == ["push", "--force-with-lease=onboard/analytics:head1", "origin",
                         "HEAD:refs/heads/onboard/analytics"]
    assert sh.ran("gh pr create") == []


def pushed_then_crashed(tmp_path, box, parent_and_tree):
    """Confirmed; then a run whose branch already holds `mine1`, pushed
    before a crash lost the record's save, with its PR open."""
    rec = resolved(tmp_path, box)
    intent(tmp_path, "confirm", {"revision": rec["revision"]})
    sh = FakeSh(infra_routes(open_prs=json.dumps(
        [{"number": 7, "url": PR_URL, "headRefOid": "mine1"}]), **NO_CI_RUN))
    sh.route("for-each-ref", "mine1\n")
    sh.route("show -s --format=%P %T mine1", parent_and_tree + "\n")
    run(box, sh)
    return sh, record(tmp_path)


def test_config_pr_adopts_its_own_push_after_a_crash(tmp_path, box, infra_token):
    """The head is the commit this run would push: main plus this proposal."""
    sh, rec = pushed_then_crashed(tmp_path, box, "base1 tree1")
    assert step(rec, "config-pr")["state"] == "verified"
    assert (rec["infra_pr"]["number"], rec["infra_pr"]["head"], rec["infra_pr"]["base"]) == (
        7, "mine1", "base1")
    assert rec["infra_pr"]["revision"] == rec["revision"]
    assert sh.ran(" push ") == [] and sh.ran("gh pr create") == []


@pytest.mark.parametrize("parent_and_tree", [
    "base1 tree-with-an-extra-file",  # any author, message or trailer
    "base0 tree1",                    # the same change on an older main
    "base1 extra1 tree1"])            # a merge commit
def test_config_pr_blocks_on_a_head_that_is_not_its_push_of_this_proposal(
        tmp_path, box, infra_token, parent_and_tree):
    sh, rec = pushed_then_crashed(tmp_path, box, parent_and_tree)
    s = step(rec, "config-pr")
    assert s["state"] == "blocked" and "mine1" in s["detail"]
    assert sh.ran(" push ") == []


def test_a_rebuild_blocks_when_someone_else_moved_the_branch(tmp_path, box, infra_token):
    confirmed(tmp_path, box, FakeSh(infra_routes(**NO_CI_RUN)))
    sh = FakeSh(infra_routes(**NO_CI_RUN))
    sh.route("rev-parse origin/main", "base2\n")
    sh.route("for-each-ref", "theirs2\n")
    run(box, sh)
    s = step(record(tmp_path), "config-merge")
    assert s["state"] == "blocked" and "theirs2" in s["detail"] and s["link"] == PR_URL
    assert sh.ran(" push ") == []


def test_config_merge_blocks_a_pr_retargeted_off_main(tmp_path, box, infra_token):
    sh = FakeSh(infra_routes(**{"gh pr view 7": pr_view("OPEN", base="release")}))
    s = step(confirmed(tmp_path, box, sh), "config-merge")
    assert s["state"] == "blocked" and "'release'" in s["detail"] and s["link"] == PR_URL
    assert "set its base back to main" in s["detail"]


def test_config_merge_blocks_a_pr_merged_into_another_base(tmp_path, box, infra_token):
    """A merge into another branch doesn't deploy."""
    sh = FakeSh(infra_routes(**{"gh pr view 7": pr_view("MERGED", base="release")}))
    rec = confirmed(tmp_path, box, sh)
    s = step(rec, "config-merge")
    assert s["state"] == "blocked" and "'release'" in s["detail"] and s["link"] == PR_URL
    assert (rec["merge_sha"], rec["merged_at"]) == ("", "")


def test_config_merge_blocks_on_a_closed_pr(tmp_path, box, infra_token):
    closed = {"gh pr view 7": json.dumps({"state": "CLOSED"})}
    s = step(confirmed(tmp_path, box, FakeSh(infra_routes(**closed))), "config-merge")
    assert s["state"] == "blocked" and "closed" in s["detail"]
    assert "for now, until `agent-ops project abandon` ships" in s["detail"]


# -- deploy -------------------------------------------------------------------

DEPLOYED = LIVE + """\
  - name: analytics
    paused: true
    board: {owner: jesdi, number: 4}
    repositories:
      - name: analytics
        repo: jesdi/analytics
        github_id: 202
        clone_path: /home/agent/repos/analytics/analytics
        worktrees_path: /home/agent/repos/analytics/analytics.worktrees
        gate_cmd: make gate
"""


def merged(tmp_path, box):
    """Confirmed, and the operator merged the infra PR at NOW."""
    rec = confirmed(tmp_path, box, FakeSh(infra_routes(**MERGED)))
    assert step(rec, "config-merge")["state"] == "verified"
    return rec


def test_deploy_waits_until_the_box_has_the_merge(tmp_path, box, infra_token):
    s = step(merged(tmp_path, box), "deploy")
    assert s["state"] == "waiting"


def test_deploy_is_verified_once_infra_has_the_merge_and_the_live_config_the_project(
        tmp_path, box, infra_token):
    merged(tmp_path, box)
    box.write_text(DEPLOYED)
    sh = FakeSh(infra_routes(**{"merge-base --is-ancestor merge1 HEAD": ""}))
    run(box, sh, now=NOW + timedelta(minutes=2))
    assert step(record(tmp_path), "deploy")["state"] == "verified"
    [(argv, _, _)] = sh.ran("merge-base")
    assert argv[:3] == ["git", "-C", str(Path.home() / "agent-ops-infra")]


@pytest.mark.parametrize("live", [LIVE, DEPLOYED.replace("paused: true", "paused: false")])
def test_deploy_waits_while_the_live_config_lacks_the_paused_project(
        tmp_path, box, infra_token, live):
    merged(tmp_path, box)
    box.write_text(live)
    run(box, FakeSh(infra_routes(**{"merge-base --is-ancestor merge1 HEAD": ""})),
        now=NOW + timedelta(minutes=2))
    assert step(record(tmp_path), "deploy")["state"] == "waiting"


def test_deploy_blocks_with_the_journal_when_the_updater_failed_after_the_merge(
        tmp_path, box, infra_token):
    merged(tmp_path, box)
    sh = FakeSh(infra_routes(**{
        "systemctl --user show agent-ops-update.service":
            updater("exit-code", NOW + timedelta(minutes=1)),
        "journalctl --user -u agent-ops-update.service": "configure.py: bad config\n"}))
    run(box, sh, now=NOW + timedelta(minutes=2))
    s = step(record(tmp_path), "deploy")
    assert s["state"] == "blocked" and "configure.py: bad config" in s["detail"]
    [(argv, _, _)] = sh.ran("journalctl")
    assert argv == ["journalctl", "--user", "-u", "agent-ops-update.service",
                    "-n", "15", "--no-pager", "-o", "cat"]


def test_deploy_ignores_an_updater_failure_from_before_the_merge(tmp_path, box, infra_token):
    merged(tmp_path, box)
    run(box, FakeSh(infra_routes(**{"systemctl --user show agent-ops-update.service":
                                    updater("exit-code", NOW - timedelta(minutes=1))})),
        now=NOW + timedelta(minutes=2))
    assert step(record(tmp_path), "deploy")["state"] == "waiting"


def test_deploy_blocks_naming_the_timer_after_15_minutes(tmp_path, box, infra_token):
    merged(tmp_path, box)
    run(box, FakeSh(infra_routes()), now=NOW + timedelta(minutes=16))
    s = step(record(tmp_path), "deploy")
    assert s["state"] == "blocked" and "agent-ops-update.timer" in s["detail"]


# -- visible ------------------------------------------------------------------

LANDED = {"merge-base --is-ancestor merge1 HEAD": ""}


class FakeGet:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return json.dumps({"projects": answer}).encode()


LISTED = [{"name": "portfolio-eval", "paused": False, "repositories": ["portfolio_eval"]},
          {"name": "analytics", "paused": True, "repositories": ["analytics"]}]


def deployed(tmp_path, box, get, now=NOW + timedelta(minutes=2)):
    """Merged, then a run at `now` that sees the deploy and asks `get`."""
    merged(tmp_path, box)
    box.write_text(DEPLOYED)
    run(box, FakeSh(infra_routes(**LANDED)), get=get, now=now)
    return record(tmp_path)


def test_visible_completes_once_the_console_lists_the_project_paused(
        tmp_path, box, infra_token, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_WEB_PORT", "9123")
    get = FakeGet(LISTED)
    rec = deployed(tmp_path, box, get)
    assert step(rec, "visible")["state"] == "verified"
    assert rec["status"] == "complete"
    assert get.calls == [("http://127.0.0.1:9123/api/projects",
                          {"Tailscale-User-Login": "agent-ops-onboard"})]


@pytest.mark.parametrize("answer", [
    ConnectionRefusedError(61, "refused"), LISTED[:1],
    [{**LISTED[1], "paused": False}]])
def test_visible_waits_while_the_console_is_down_or_lacks_the_project(
        tmp_path, box, infra_token, answer):
    rec = deployed(tmp_path, box, FakeGet(answer))
    assert step(rec, "visible")["state"] == "waiting"
    assert rec["status"] == "open"


def test_visible_blocks_naming_the_web_unit_after_15_minutes(tmp_path, box, infra_token):
    deployed(tmp_path, box, FakeGet(LISTED[:1]))
    run(box, FakeSh(), get=FakeGet(LISTED[:1]), now=NOW + timedelta(minutes=16))
    assert step(record(tmp_path), "visible")["state"] == "waiting"  # 14 since the deploy
    run(box, FakeSh(), get=FakeGet(LISTED[:1]), now=NOW + timedelta(minutes=18))
    s = step(record(tmp_path), "visible")
    assert s["state"] == "blocked" and "agent-ops-web.service" in s["detail"]


def test_a_complete_record_is_left_alone(tmp_path, box, infra_token):
    deployed(tmp_path, box, FakeGet(LISTED))
    run(box, FakeSh({"": AssertionError("no call expected")}), get=no_get)
    assert record(tmp_path)["status"] == "complete"


def test_load_record_gives_the_typed_record_the_file_holds(tmp_path, box, infra_token):
    merged(tmp_path, box)
    rec = onboard.load_record(state(tmp_path), "analytics")
    assert rec.step("config-merge").state is onboard.StepState.VERIFIED
    assert isinstance(rec.infra_pr, onboard.InfraPr) and rec.merge_sha == "merge1"
    assert onboard.asdict(rec) == record(tmp_path)
