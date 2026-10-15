"""Rejection branches of the project/repository validators in dispatcher.config."""
import pytest

from dispatcher.config import _normalize_repo, _project, _project_repository, _projects

REPO = {"repo": "o/r", "clone_path": "/c", "worktrees_path": "/w",
        "rank_cmd": "rank", "gate_cmd": "make gate"}
PROJECT = {"key": "k", "name": "n", "board_owner": "o", "board_number": 1,
           "status_field_id": "f", "status_ready_option_id": "r",
           "status_in_progress_option_id": "i", "repositories": [REPO]}


def test_valid_project_parses():
    p = _project(PROJECT, 2)
    assert p.key == "k" and not p.enabled and p.repositories[0].repo == "o/r"


@pytest.mark.parametrize("patch,msg", [
    ({"clone_path": 3}, "clone_path must be a string"),
    ({"gate_cmd": "  "}, "gate_cmd is required"),
    ({"max_active": "2"}, "max_active must be an integer"),
    ({"max_active": True}, "max_active must be an integer"),
    ({"bogus": 1}, "unexpected keyword"),
])
def test_repository_rejections(patch, msg):
    with pytest.raises(ValueError, match=msg):
        _project_repository({**REPO, **patch}, "k")


def test_repository_accepts_int_max_active():
    assert _project_repository({**REPO, "max_active": 2}, "k").max_active == 2


@pytest.mark.parametrize("patch,msg", [
    ({"repositories": []}, "nonempty list"),
    ({"repositories": "x"}, "nonempty list"),
    ({"repositories": ["x"]}, "must be a mapping"),
    ({"enabled": "yes"}, "enabled must be a boolean"),
    ({"board_number": "1"}, "board_number must be an integer"),
    ({"board_number": True}, "board_number must be an integer"),
    ({"bogus": 1}, "unexpected keyword"),
    ({"board_owner": 17}, "board_owner must be a string"),
    ({"name": None}, "name must be a string"),
    ({"status_field_id": False}, "status_field_id must be a string"),
])
def test_project_rejections(patch, msg):
    with pytest.raises(ValueError, match=msg):
        _project({**PROJECT, **patch}, 2)


@pytest.mark.parametrize("projects_raw", [None, 42, [None], ["x"]])
def test_projects_rejects_malformed_container(projects_raw):
    with pytest.raises(ValueError, match="projects"):
        _projects({"projects": projects_raw}, 2, [])


def test_normalize_repo_strips_trailing_slash_before_git_suffix():
    assert _normalize_repo("https://github.com/example/shop.git/") == "example/shop"
