"""A project's board as onboarding's board step and the dispatcher's
reconcile pass see it (#230): `survey` reads it fresh, with the project's
issues and labels, and `sync` feeds it. Both callers bring `Credentials`: the
board token for Projects v2, the repository credentials for issues and labels."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from dispatcher import board, labels


@dataclass(frozen=True)
class Credentials:
    board: board.Gh   # Projects v2, with the board token
    repo: board.Gh    # issues and labels, with the repository credentials


@dataclass(frozen=True)
class Survey:
    fields: board.Fields
    clash: str                     # a BoardError naming each clash; "" when none
    gaps: board.Gaps | None        # None while a clash hides them
    outside: list[str]             # cards of issues and PRs outside the project
    unreadable: int                # cards the board token can't read, joining no member issue
    issues: list[dict]             # every issue of every repository
    items: list[board.Item]

    def blocker(self) -> str:
        """Why the board can't be the project's (#230 D1, D4); "" when it can."""
        if self.clash:
            return self.clash
        if not self.outside:
            return ""
        more = f" and {len(self.outside) - 10} more" if len(self.outside) > 10 else ""
        return (f"board {self.fields.project} holds cards from outside the project: "
                f"{', '.join(self.outside[:10])}{more}. Nothing was removed: choose another "
                "board or a new one")

    def review(self) -> list[str]:
        """What preparing the board will add, for the operator to confirm."""
        notes = [f"{self.unreadable} card{'s' * (self.unreadable > 1)} the board token "
                 "can't read: they stay as they are"] if self.unreadable else []
        return [*(self.gaps.lines() if self.gaps else []), *notes]

    def uncarded(self, issues: list[dict]) -> list[dict]:
        """The open ones of `issues` no card joins. A card the board token
        can't read joins no issue unless its title is unique, so a fresh add
        may be listed."""
        carded = board.cards(self.items, issues)
        return [i for i in issues if i["state"] == "OPEN"
                and (i["repository"].lower(), i["number"]) not in carded]


def label_lines(missing: dict[str, list[str]]) -> list[str]:
    return [f"add labels on {repo}: {', '.join(sorted(names))}"
            for repo, names in missing.items() if names]


def missing_labels(repos: list[str], tracks, gh: Credentials) -> dict[str, list[str]]:
    return {repo: [x[0] for x in labels.missing(repo, tracks, gh.repo)] for repo in repos}


def survey(owner: str, number: int, repos: list[str], gh: Credentials) -> Survey:
    """Board `number` of `owner` read live, never from the fields cache, with
    every issue of `repos`."""
    board.forget(owner, number)
    f = board.fields(owner, number, gh.board)
    try:
        gaps, clash = board.gaps(f), ""
    except board.BoardError as e:
        gaps, clash = None, str(e)
    issues = [i for repo in repos for i in board.repo_issues(repo, gh.repo)]
    items = board.fetch_items(f.id, gh.board)
    outside, unreadable = board.foreign(items, {x.lower() for x in repos},
                                        {i["title"] for i in issues})
    return Survey(f, clash, gaps, outside, unreadable, issues, items)


@dataclass(frozen=True)
class Synced:
    carded: board.Carded
    labels: list[str]      # "<repo>: label <name>" per label created
    errors: list[str]      # what failed; the rest was still done

    def lines(self) -> list[str]:
        return [*self.labels, *(f"card added: {url}" for url in self.carded.added),
                *(f"Inbox set: {url}" for url in self.carded.inboxed)]


def sync(s: Survey, repos: list[str], tracks, gh: Credentials) -> Synced:
    """Cards for the open issues, then each repository's labels. One write
    failing never holds back another; its error is kept, with every write
    that landed."""
    try:
        carded = board.card_open_issues(s.fields, s.items, s.issues, gh.board)
        errors = [f"card {e}" for e in carded.errors]
    except board.BoardError as e:    # no Inbox: nothing was written
        carded, errors = board.Carded([], []), [f"cards: {e}"]
    made = []
    for repo in repos:
        try:
            wanted = labels.missing(repo, tracks, gh.repo)
        except subprocess.SubprocessError as e:
            errors.append(f"{repo} labels: {_reason(e)}")
            continue
        for label in wanted:
            try:
                labels.create(repo, label, gh.repo)
                made.append(f"{repo}: label {label[0]}")
            except subprocess.SubprocessError as e:
                errors.append(f"{repo} label {label[0]}: {_reason(e)}")
    return Synced(carded, made, errors)


def _reason(e: Exception) -> str:
    return (getattr(e, "stderr", None) or str(e)).strip()[-500:]
