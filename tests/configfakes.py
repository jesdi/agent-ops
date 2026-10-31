"""Builders for config objects: a one-repository project, and nested
replaces of a repository's project or board."""
from dataclasses import replace

from dispatcher.config import Board, Project, Repository


def make_project(name="alpha", **kw) -> Project:
    board = Board(owner="jesdi", number=1)
    return Project(name=name, board=board, **kw)


def with_project(repository: Repository, **kw) -> Repository:
    """`repository` with its project's fields replaced by `kw`."""
    return replace(repository, project=replace(repository.project, **kw))


def with_board(repository: Repository, **kw) -> Repository:
    """`repository` with its project's board fields replaced by `kw`."""
    return with_project(repository, board=replace(repository.project.board, **kw))
