"""A project's board as onboarding's board step and the dispatcher's
reconcile pass see it (#230): `survey` reads it fresh, with the project's
issues and labels, and `sync` feeds it. Both callers bring a `Gh`: the board
token for Projects v2, the repository credentials for issues and labels."""
from __future__ import annotations

from dataclasses import dataclass

from dispatcher import board, labels


@dataclass(frozen=True)
class Gh:
    board: board.Gh   # Projects v2, with the board token
    repo: board.Gh    # issues and labels, with the repository credentials


@dataclass(frozen=True)
class Survey:
    fields: board.Fields
    clash: str                     # a BoardError naming each clash; "" when none
    gaps: board.Gaps
    outside: list[str]             # cards of issues and PRs outside the project
    unreadable: int                # cards the board token can't read, joining no member issue
    missing_labels: dict[str, list[str]]   # repository -> labels it lacks
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
        return [*self.gaps.lines(), *label_lines(self.missing_labels), *notes]

    def uncarded(self) -> list[dict]:
        """Open issues no card joins. A card the board token can't read joins
        no issue unless its title is unique, so a fresh add may be listed."""
        carded = board.cards(self.items, self.issues)
        return [i for i in self.issues if i["state"] == "OPEN"
                and (i["repository"].lower(), i["number"]) not in carded]


def label_lines(missing: dict[str, list[str]]) -> list[str]:
    return [f"add labels on {repo}: {', '.join(sorted(names))}"
            for repo, names in missing.items() if names]


def missing_labels(repos: list[str], tracks, gh: Gh) -> dict[str, list[str]]:
    return {repo: [x[0] for x in labels.missing(repo, tracks, gh.repo)] for repo in repos}


def survey(owner: str, number: int, repos: list[str], tracks, gh: Gh) -> Survey:
    """Board `number` of `owner` read live, never from the fields cache, with
    every issue and the missing labels of `repos`."""
    board.forget(owner, number)
    f = board.fields(owner, number, gh.board)
    try:
        gaps, clash = board.gaps(f), ""
    except board.BoardError as e:
        gaps, clash = board.Gaps(), str(e)
    issues = [i for repo in repos for i in board.repo_issues(repo, gh.repo)]
    items = board.fetch_items(f.id, gh.board)
    outside, unreadable = board.foreign(items, {x.lower() for x in repos},
                                        {i["title"] for i in issues})
    return Survey(f, clash, gaps, outside, unreadable, missing_labels(repos, tracks, gh),
                  issues, items)


@dataclass(frozen=True)
class Synced:
    cards: list[str]       # urls of the issues given a card
    labels: list[str]      # "<repo>: label <name>" per label created

    def lines(self) -> list[str]:
        return [*self.labels, *(f"card added: {url}" for url in self.cards)]


def sync(s: Survey, tracks, gh: Gh) -> Synced:
    """Cards for the open issues, then the labels: a repository that refuses
    a label can't hold the cards back."""
    added = board.card_open_issues(s.fields, s.items, s.issues, gh.board)
    made = [f"{repo}: label {name}" for repo in s.missing_labels
            for name in labels.ensure(repo, tracks, gh.repo)]
    return Synced(added, made)
