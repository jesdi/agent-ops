"""Future issues keep arriving (#230 D8): every dispatcher pass gives each
open issue with no card a card in Inbox, and every repository the labels it
lacks, for every project, paused ones included."""
import subprocess

import pytest

import dispatcher.main as main
from dispatcher import github, labels
from dispatcher.board import BoardError
from dispatcher.github import GitHubClient
from tests.configfakes import with_project
from tests.fake_github import FakeGitHub
from tests.test_main import FakeGitHub as FakeClient
from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
from tests.test_multi_project_capacity import two_repository_cfg


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    fake.board("jesdi", 1)
    monkeypatch.setattr(github, "_run", lambda args, cwd=None, env=None: fake(args, cwd, env))
    return fake


def test_reconcile_adds_a_card_for_a_new_issue_and_keeps_triage(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    b = gh.boards[("jesdi", 1)]
    triaged = gh.issue("jesdi/portfolio_eval", 1)
    gh.card(b, triaged, "Ready")
    later = gh.issue("jesdi/portfolio_eval", 2)
    gh.issue("jesdi/portfolio_eval", 3, state="CLOSED")

    lines = GitHubClient().reconcile_board(c.repositories[:1], {"standard"})

    assert gh.status(b, triaged) == "Ready" and gh.status(b, later) == "Inbox"
    assert len(b["items"]) == 2
    assert "card added: https://github.com/jesdi/portfolio_eval/issues/2" in lines
    assert "track:standard" in gh.labels["jesdi/portfolio_eval"]
    assert GitHubClient().reconcile_board(c.repositories[:1], {"standard"}) == []


def test_a_track_added_later_reaches_every_repository(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    lines = GitHubClient().reconcile_board(c.repositories[:1], {"standard", "fast"})
    assert lines == ["jesdi/portfolio_eval: label track:fast"]


def test_a_dry_run_writes_nothing(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    gh.issue("jesdi/portfolio_eval", 2)
    assert GitHubClient(dry_run=True).reconcile_board(c.repositories[:1], {"standard"}) == []
    assert gh.boards[("jesdi", 1)]["items"] == [] and gh.labels == {}


class Reconciling(FakeClient):
    def __init__(self, fails=(), **kw):
        super().__init__(**kw)
        self.reconciled, self.fails = [], fails

    def reconcile_board(self, repositories, tracks):
        project = repositories[0].project.name
        self.reconciled.append((project, sorted(tracks)))
        if project in self.fails:
            raise RuntimeError("board unreachable")
        return [f"card added: {project}"]


def test_every_pass_reconciles_every_project_paused_included(tmp_path, monkeypatch, capsys):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_repository_cfg(tmp_path)
    c.repositories[1] = with_project(c.repositories[1], paused=True)
    client = Reconciling(fails={c.repositories[0].project.name})
    main.run_pass(c, deps(client, FakeSessions(alive=set())))
    assert [p for p, _ in client.reconciled] == [r.project.name for r in c.repositories]
    assert all(t == sorted(labels.tracks(c)) and t for _, t in client.reconciled)
    err = capsys.readouterr()
    assert "board unreachable" in err.err
    assert "card added: factorial" in err.out + err.err


def test_a_board_without_inbox_gets_no_card_without_a_status(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    b = gh.boards[("jesdi", 1)]
    b["fields"]["Status"]["options"] = [o for o in b["fields"]["Status"]["options"] if o["name"] != "Inbox"]
    gh.issue("jesdi/portfolio_eval", 2)
    with pytest.raises(BoardError, match="Inbox"):
        GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    assert b["items"] == []


def test_a_repository_refusing_labels_still_gets_its_cards(tmp_path, gh, monkeypatch):
    c = two_repository_cfg(tmp_path)
    later = gh.issue("jesdi/portfolio_eval", 2)

    def no_labels(args, cwd=None, env=None):
        if args[1:3] == ["label", "create"]:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 403")
        return gh(args, cwd, env)
    monkeypatch.setattr(github, "_run", no_labels)
    with pytest.raises(subprocess.CalledProcessError):
        GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    assert gh.status(gh.boards[("jesdi", 1)], later) == "Inbox"
