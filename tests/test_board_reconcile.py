"""Future issues keep arriving (#230 D8): every dispatcher pass gives each
open issue with no card a card in Inbox, and every repository the labels it
lacks, for every project, paused ones included."""
import subprocess

import pytest

import dispatcher.main as main
from dispatcher import github, labels
from dispatcher import project_board
from dispatcher.board import Carded
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

    lines = GitHubClient().reconcile_board(c.repositories[:1], {"standard"}).lines()

    assert gh.status(b, triaged) == "Ready" and gh.status(b, later) == "Inbox"
    assert len(b["items"]) == 2
    assert "card added: https://github.com/jesdi/portfolio_eval/issues/2" in lines
    assert "track:standard" in gh.labels["jesdi/portfolio_eval"]
    assert GitHubClient().reconcile_board(c.repositories[:1], {"standard"}).lines() == []


def test_a_track_added_later_reaches_every_repository(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    lines = GitHubClient().reconcile_board(c.repositories[:1], {"standard", "fast"}).lines()
    assert lines == ["jesdi/portfolio_eval: label track:fast"]


def test_a_dry_run_writes_nothing(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    gh.issue("jesdi/portfolio_eval", 2)
    assert GitHubClient(dry_run=True).reconcile_board(c.repositories[:1], {"standard"}).lines() == []
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
        return project_board.Synced(Carded([f"https://example/{project}"], []), [], [])


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
    assert "card added: https://example/factorial" in err.out


def test_a_board_without_inbox_gets_no_card_but_still_its_labels(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    b = gh.boards[("jesdi", 1)]
    b["fields"]["Status"]["options"] = [o for o in b["fields"]["Status"]["options"] if o["name"] != "Inbox"]
    gh.issue("jesdi/portfolio_eval", 2)
    synced = GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    assert b["items"] == []
    assert len(synced.errors) == 1 and "Inbox" in synced.errors[0]
    assert "track:standard" in gh.labels["jesdi/portfolio_eval"]


def test_a_board_without_inbox_and_nothing_to_card_is_fine(tmp_path, gh):
    c = two_repository_cfg(tmp_path)
    b = gh.boards[("jesdi", 1)]
    b["fields"]["Status"]["options"] = [o for o in b["fields"]["Status"]["options"] if o["name"] != "Inbox"]
    assert GitHubClient().reconcile_board(c.repositories[:1], {"standard"}).errors == []


def test_a_repository_refusing_labels_still_gets_its_cards(tmp_path, gh, monkeypatch):
    c = two_repository_cfg(tmp_path)
    later = gh.issue("jesdi/portfolio_eval", 2)

    def no_labels(args, cwd=None, env=None):
        if args[1:3] == ["label", "create"]:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 403")
        return gh(args, cwd, env)
    monkeypatch.setattr(github, "_run", no_labels)
    synced = GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    assert gh.status(gh.boards[("jesdi", 1)], later) == "Inbox"
    assert synced.carded.added == [later["url"]]
    assert synced.errors and "HTTP 403" in synced.errors[0]


def test_an_unreadable_label_list_still_lets_cards_in(tmp_path, gh, monkeypatch):
    c = two_repository_cfg(tmp_path)
    later = gh.issue("jesdi/portfolio_eval", 2)

    def no_label_list(args, cwd=None, env=None):
        if args[1:3] == ["label", "list"]:
            raise subprocess.CalledProcessError(1, args, stderr="rate limited")
        return gh(args, cwd, env)
    monkeypatch.setattr(github, "_run", no_label_list)
    synced = GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    assert gh.status(gh.boards[("jesdi", 1)], later) == "Inbox"
    assert "rate limited" in synced.errors[0]


def test_one_issue_failing_never_stops_the_others_and_every_write_is_reported(tmp_path, gh, monkeypatch):
    c = two_repository_cfg(tmp_path)
    b = gh.boards[("jesdi", 1)]
    bad, good = gh.issue("jesdi/portfolio_eval", 2), gh.issue("jesdi/portfolio_eval", 3)
    bare = gh.issue("jesdi/portfolio_eval", 4)
    gh.card(b, bare)
    gh.labels["jesdi/portfolio_eval"] = {}

    def flaky(args, cwd=None, env=None):
        line = " ".join(args)
        if f"c={bad['id']}" in line or (args[1:3] == ["label", "create"] and args[3] == "ci"):
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 502")
        return gh(args, cwd, env)
    monkeypatch.setattr(github, "_run", flaky)
    synced = GitHubClient().reconcile_board(c.repositories[:1], {"standard"})
    assert synced.carded.added == [good["url"]] and synced.carded.inboxed == [bare["url"]]
    assert gh.status(b, good) == gh.status(b, bare) == "Inbox" and gh.item_of(b, bad) is None
    assert any(bad["url"] in e for e in synced.errors) and any("label ci" in e for e in synced.errors)
    assert "jesdi/portfolio_eval: label spec-ready" in synced.lines()
