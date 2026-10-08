"""The nightly sweep prioritizes: it scores issues nobody has scored, makes
them Ready, repairs stale cards, and leaves a person's scores alone."""
import json
import subprocess
from unittest.mock import patch

import pytest

from dispatcher import triage
from dispatcher.config import Config, Target
from tests.test_board import SCHEMA, _issue, _node, _page
from tests.usagefakes import session_usage

OLD = "2026-07-29T00:00:00Z"
LABELS = [{"name": n, "description": ""} for n in ("bug", "auto", "frontend")]


class Gh:
    """Every gh call a sweep makes against one repo, answered from a small
    model of the repo and its board."""

    def __init__(self, nodes, issues, window=(), board_rc=0):
        self.nodes, self.issues, self.window = nodes, issues, list(window)
        self.board_rc = board_rc
        self.mutations, self.edits = [], []

    def __call__(self, args, capture_output=True, text=True, timeout=120,
                 env=None):
        def ok(out=""):
            return subprocess.CompletedProcess(args, 0, out, "")
        query = next((a for a in args if a.startswith("query=")), "")
        if "mutation" in query:
            self.mutations.append(" ".join(args))
            return ok()
        if args[1:3] == ["api", "graphql"]:
            return subprocess.CompletedProcess(
                args, self.board_rc, _page(self.nodes), "API rate limit exceeded")
        if args[1] == "api":
            return subprocess.CompletedProcess(args, 1, "", "not an org")
        if args[1:3] == ["label", "list"]:
            return ok(json.dumps(LABELS))
        if args[1:3] == ["issue", "edit"]:
            self.edits.append(args)
            return ok()
        if args[1:3] == ["issue", "view"]:
            number = int(args[3])
            if "comments" in args:
                return ok(json.dumps({"comments": []}))
            return ok(json.dumps({"number": number, "title": f"issue {number}",
                                  "body": "b", "labels": [],
                                  "author": {"login": "alice"}}))
        if args[1:3] == ["issue", "list"]:
            if "--search" in args:
                return ok(json.dumps([
                    {"number": n, "title": f"issue {n}", "body": "b",
                     "labels": [], "author": {"login": "alice"}}
                    for n in self.window]))
            if "all" in args:
                return ok(json.dumps(self.issues))
            return ok(json.dumps([{"number": i["number"], "title": i["title"]}
                                  for i in self.issues if i["state"] == "OPEN"]))
        raise AssertionError(f"unexpected gh call: {args}")


class Notifier:
    def __init__(self):
        self.lines = []

    def send(self, template, **ctx):
        self.lines += ctx["lines"]


class Deps:
    def __init__(self):
        self.notifier = Notifier()


@pytest.fixture
def cfg(tmp_path):
    clone = tmp_path / "clone"
    (clone / ".backlog").mkdir(parents=True)
    (clone / ".backlog" / "project-meta.json").write_text(json.dumps(SCHEMA))
    state = tmp_path / "state"
    target = Target(
        name="a", repo="o/a", clone_path=str(clone), worktrees_path="/w",
        rank_cmd="r", project_number=1, project_owner="o",
        status_field_id="F_STATUS", status_ready_option_id="o_ready",
        status_in_progress_option_id="o_prog", gate_cmd="make gate")
    cfg = Config(state_dir=str(state), capacity=2, session_memory="1500m",
                 session_cpus="2", targets=[target])
    triage.save_cursors(state, {"o/a": OLD})
    return cfg


def _sweep(cfg, gh, decisions):
    """Run the sweep; returns (report lines, the context the session saw)."""
    seen = {}

    def session(cfg, repo, blob, started_date, entry, run=None):
        seen.update(blob)
        return decisions

    deps = Deps()
    with patch.object(triage, "fetch_all",
                      return_value={"anthropic": session_usage(0.1)}), \
         patch.object(triage, "_run_session", side_effect=session):
        triage.run_sweep(cfg, deps, run=gh)
    return deps.notifier.lines, seen


def test_an_issue_untouched_since_the_cursor_is_still_scored_and_made_ready(cfg):
    # #1 sits unscored on the board and was not updated since the last sweep.
    gh = Gh([_node("I1", 1)], [_issue(1, "issue 1")])
    lines, seen = _sweep(cfg, gh, {"issues": [
        {"number": 1, "add_labels": ["auto"], "impact": 4, "effort": 2,
         "area": "frontend"}]})
    (issue,) = seen["issues"]
    assert issue["number"] == 1 and issue["needs_score"] is True
    assert seen["areas"] == ["frontend", "infra"]
    (mutation,) = gh.mutations
    assert "i=I1" in mutation
    assert 'fieldId:"F_SCORE",value:{number:2.0}' in mutation
    assert 'singleSelectOptionId:"o_ready"' in mutation
    assert "o/a: 1 labeled, 1 scored" in lines[0]
    assert triage.load_cursors(cfg.state_dir)["o/a"] != OLD


def test_a_score_a_person_set_is_shown_but_never_overwritten(cfg):
    # #3 was updated (it is in the window) but a person already scored it.
    gh = Gh([_node("I3", 3, status="Ready", impact=5, effort=1, area="infra")],
            [_issue(3, "issue 3")], window=[3])
    lines, seen = _sweep(cfg, gh, {"issues": [
        {"number": 3, "impact": 1, "effort": 5, "area": "frontend"}]})
    (issue,) = seen["issues"]
    assert issue["needs_score"] is False
    assert issue["board"] == {"status": "Ready", "impact": 5, "effort": 1,
                              "area": "infra"}
    assert gh.mutations == []
    assert any("rejected: #3: not awaiting a score" in l for l in lines)


def test_a_closed_issue_with_an_open_card_is_repaired_without_a_session(cfg):
    gh = Gh([_node("I8", 8, status="In progress")],
            [_issue(8, "issue 8", state="CLOSED", reason="NOT_PLANNED")])
    lines, seen = _sweep(cfg, gh, {"issues": []})
    assert seen == {}  # nothing to triage: no session ran
    assert len(gh.mutations) == 1 and "o_wont" in gh.mutations[0]
    assert lines == ["o/a: #8 closed: board status -> Wont do",
                     "o/a: nothing new"]


def test_an_open_issue_missing_from_the_board_is_reported(cfg):
    gh = Gh([], [_issue(9, "issue 9")])
    lines, _ = _sweep(cfg, gh, {"issues": []})
    assert "o/a: not on the board, cannot be scored: #9" in lines


def test_when_the_board_cannot_be_read_labels_are_still_triaged(cfg):
    gh = Gh([], [_issue(1, "issue 1")], window=[1], board_rc=1)
    lines, seen = _sweep(cfg, gh, {"issues": [
        {"number": 1, "add_labels": ["bug"]}]})
    assert "needs_score" not in seen["issues"][0]
    assert len(gh.edits) == 1
    assert lines[0].startswith("o/a: board unavailable, labels only")
    assert "API rate limit exceeded" in lines[0]
    assert "o/a: 1 labeled, 0 scored" in lines[1]


def test_the_sweep_tab_receives_the_board_token(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    assert triage._launch_env()["GH_PROJECT_TOKEN"] == "classic-tok"


def test_a_repo_that_is_not_a_target_gets_labels_only(cfg):
    from dataclasses import replace
    cfg = replace(cfg, targets=[], infra_repo="o/infra")
    triage.save_cursors(cfg.state_dir, {"o/infra": OLD})
    gh = Gh([], [_issue(1, "issue 1")], window=[1])
    lines, seen = _sweep(cfg, gh, {"issues": [
        {"number": 1, "add_labels": ["bug"], "impact": 3, "effort": 1,
         "area": "infra"}]})
    assert "needs_score" not in seen["issues"][0] and "areas" not in seen
    assert gh.mutations == []
    assert any("rejected: #1: this repo has no board to score on" in l
               for l in lines)
