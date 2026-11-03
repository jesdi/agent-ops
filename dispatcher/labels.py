"""The one label set every repository of every project carries (#230 D2):
the backlog skill's taxonomy, names, colors and descriptions unchanged, plus
`track:<name>` for every configured track. Labels are only ever created:
one that exists under the same name is left as it is."""
from __future__ import annotations

import json
from typing import Callable

from dispatcher.config import Config
from dispatcher.models import TRACK_LABEL_PREFIX

TAXONOMY = (
    ("inbox", "BFD4F2", "Un-triaged capture"),
    ("bug", "d73a4a", "Something is broken or behaves incorrectly"),
    ("enhancement", "a2eeef", "New feature or improvement to existing behavior"),
    ("documentation", "0075ca", "Docs, READMEs, comments, guides"),
    ("question", "d876e3", "Open question or decision needed, not yet actionable work"),
    ("frontend", "fbca04", "UI, components, styling, client/browser behavior"),
    ("backend", "1d76db", "Server logic, APIs, data models, services"),
    ("infra", "c2e0c6", "Deployment, hosting, environments, tooling, build"),
    ("ci", "bfdadc", "CI/CD pipelines, workflows, checks"),
    ("security", "ee0701", "Vulnerabilities, auth, permissions, hardening"),
    ("performance", "f9d0c4", "Speed, memory, efficiency, scalability"),
    ("testing", "0e8a16", "Test coverage, test infrastructure, flaky tests"),
    ("dependencies", "0366d6", "Upgrading or managing third-party dependencies"),
    ("auto", "5319E7", "Suitable for the unattended agents pipeline"),
    ("human-required", "B60205", "Needs heavy human interaction before automation"),
    ("spec-ready", "006B75", "Issue body carries a settled design; the pipeline skips its interview"),
)
TRACK_COLOR = "ededed"

Gh = Callable[[list[str]], str]


def tracks(cfg: Config) -> frozenset[str]:
    """Every configured track: the global policy's and each project's own."""
    policies = [cfg.models, *(p.models for p in cfg.projects if p.models is not None)]
    return frozenset(name for p in policies for name in p.tracks)


def wanted(track_names) -> list[tuple[str, str, str]]:
    return [*TAXONOMY, *((f"{TRACK_LABEL_PREFIX}{t}", TRACK_COLOR, f"Runs on the {t} model track")
                         for t in sorted(track_names))]


def missing(repo: str, track_names, gh: Gh) -> list[tuple[str, str, str]]:
    """The labels `repo` lacks; GitHub label names are case-insensitive."""
    have = {x["name"].lower() for x in json.loads(gh(
        ["label", "list", "--repo", repo, "--json", "name", "--limit", "1000"]))}
    return [x for x in wanted(track_names) if x[0].lower() not in have]


def ensure(repo: str, track_names, gh: Gh) -> list[str]:
    """Create the labels `repo` lacks; their names."""
    made = []
    for name, color, description in missing(repo, track_names, gh):
        gh(["label", "create", name, "--repo", repo, "--color", color,
            "--description", description])
        made.append(name)
    return made
