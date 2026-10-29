"""Builders for config objects: a one-repository project, and nested
replaces of a repository's project or board."""
from dataclasses import replace

from dispatcher.config import Board, Project, Repository


def make_project(name="alpha", rank_cmd="false", **kw) -> Project:
    board = Board(owner="jesdi", number=1, status_field_id="F",
                  status_ready_option_id="R", status_in_progress_option_id="I")
    return Project(name=name, rank_cmd=rank_cmd, board=board, **kw)


def with_project(repository: Repository, **kw) -> Repository:
    """`repository` with its project's fields replaced by `kw`."""
    return replace(repository, project=replace(repository.project, **kw))


def with_board(repository: Repository, **kw) -> Repository:
    """`repository` with its project's board fields replaced by `kw`."""
    return with_project(repository, board=replace(repository.project.board, **kw))
