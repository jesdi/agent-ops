"""The Projects-v2 board, read and written with narrow GraphQL.

`gh project item-list` asks for every field and several nested connections
per item: on a ~100-item board one call costs on the order of 100 of the
5000 GraphQL points GitHub allows per user per hour, and the dispatcher used
to make it on every status write. The query here costs about 1 point per 100
items.

Two tokens are in play on the box: the stored fine-grained auth reads the
repo, and the classic project-scope token (GH_PROJECT_TOKEN) reads and writes
the board. The project token cannot expand a private repo's issues, so an
item's issue number can be absent; the title is then the join key (GitHub
syncs linked-item titles to issue titles)."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable

GH_TIMEOUT = 120
UNTRIAGED_STATUSES = frozenset({None, "Inbox"})
OPEN_STATUSES = frozenset({None, "Inbox", "Ready", "In progress"})

_ITEMS_QUERY = """
query($project: ID!, $cursor: String) {
  node(id: $project) { ... on ProjectV2 { items(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      type
      content { ... on Issue { number title url repository { nameWithOwner } } }
      title: fieldValueByName(name: "Title") { ... on ProjectV2ItemFieldTextValue { text } }
      status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      area: fieldValueByName(name: "Area") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      impact: fieldValueByName(name: "Impact") { ... on ProjectV2ItemFieldNumberValue { number } }
      effort: fieldValueByName(name: "Effort") { ... on ProjectV2ItemFieldNumberValue { number } }
      boost: fieldValueByName(name: "Boost") { ... on ProjectV2ItemFieldNumberValue { number } }
    }
  } } }
}
"""


class BoardError(Exception):
    pass


def project_env() -> dict[str, str] | None:
    """User-owned Projects v2 are invisible to fine-grained PATs, so board
    calls run with GH_TOKEN set to the classic project-scope token
    (GH_PROJECT_TOKEN); every other gh call keeps the stored auth."""
    token = os.environ.get("GH_PROJECT_TOKEN")
    return {**os.environ, "GH_TOKEN": token} if token else None


@dataclass(frozen=True)
class Item:
    id: str
    number: int | None  # None: the token cannot see the linked issue
    title: str  # the issue's own title, else the board's Title field
    status: str | None
    impact: float | None
    effort: float | None
    area: str | None
    boost: float | None = None
    url: str = ""  # "" when the token cannot see the linked issue
    repository: str = ""  # "owner/name", "" likewise
    kind: str | None = None  # ISSUE, REDACTED, DRAFT_ISSUE or PULL_REQUEST


def _value(node: dict, alias: str):
    return next(iter((node.get(alias) or {}).values()), None)


def _item(node: dict) -> Item:
    content = node.get("content") or {}
    return Item(
        id=node["id"],
        number=content.get("number"),
        title=content.get("title") or _value(node, "title") or "",
        status=_value(node, "status"),
        impact=_value(node, "impact"),
        effort=_value(node, "effort"),
        area=_value(node, "area"),
        boost=_value(node, "boost"),
        url=content.get("url") or "",
        repository=(content.get("repository") or {}).get("nameWithOwner") or "",
        kind=node.get("type"))


def fetch_items(project_id: str, gh: Callable[[list[str]], str]) -> list[Item]:
    """Every item on the board. `gh` runs one gh command (argv without the
    leading "gh") with the board token and returns its stdout."""
    items: list[Item] = []
    cursor = None
    while True:
        args = ["api", "graphql", "-f", f"query={_ITEMS_QUERY}",
                "-f", f"project={project_id}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        page = json.loads(gh(args))["data"]["node"]["items"]
        items += [_item(node) for node in page["nodes"]]
        if not page["pageInfo"]["hasNextPage"]:
            return items
        cursor = page["pageInfo"]["endCursor"]


def _unique(pairs) -> dict:
    """key -> value for the keys that occur exactly once."""
    seen: dict = {}
    for key, value in pairs:
        seen.setdefault(key, []).append(value)
    return {key: values[0] for key, values in seen.items() if len(values) == 1}


def join(items: list[Item], issues: list[dict]) -> dict[int, Item]:
    """issue number -> its board item. `issues` rows carry number + title.
    An item without a number joins by title; a title shared by two issues or
    two items is ambiguous and joins nothing."""
    joined = {item.number: item for item in items if item.number is not None}
    numbers = _unique((row.get("title") or "", row["number"]) for row in issues)
    untitled = _unique((i.title, i) for i in items if i.number is None)
    for title, item in untitled.items():
        if title in numbers:
            joined.setdefault(numbers[title], item)
    return joined


# -- the live board's fields ---------------------------------------------------


_FIELDS_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectV2(number: $number) {
    id
    fields(first: 100) { nodes {
      ... on ProjectV2FieldCommon { id name }
      ... on ProjectV2SingleSelectField { options { id name } } } }
  } } }
}
"""


@dataclass(frozen=True)
class Fields:
    """A board's node id and its field and option ids by name, read from the
    live board. GitHub gives options new ids whenever they are edited, so a
    copy kept in a file goes stale (#237, #242). A name the board lacks is a
    BoardError naming the project, the field and the option."""
    project: str  # "owner/number"
    id: str  # the ProjectV2 node id
    by_name: dict[str, tuple[str, dict[str, str]]]  # field -> (id, options)

    def field(self, name: str) -> str:
        if name not in self.by_name:
            raise BoardError(f"project {self.project}: no field {name!r}")
        return self.by_name[name][0]

    def options(self, field: str) -> dict[str, str]:
        self.field(field)
        return self.by_name[field][1]

    def option(self, field: str, name: str) -> str:
        options = self.options(field)
        if name not in options:
            raise BoardError(f"project {self.project}: field {field!r} has "
                             f"no option {name!r}")
        return options[name]


_fields: dict[tuple[str, int], Fields] = {}


def fields(owner: str, number: int, gh: Callable[[list[str]], str]) -> Fields:
    """Project `number` of `owner`, read once per process: every board write
    resolves its ids here. `gh` as for fetch_items."""
    if (owner, number) not in _fields:
        out = gh(["api", "graphql", "-f", f"query={_FIELDS_QUERY}",
                  "-f", f"owner={owner}", "-F", f"number={number}"])
        project = (json.loads(out)["data"].get("repositoryOwner")
                   or {}).get("projectV2")
        if not project:
            raise BoardError(f"project {owner}/{number}: not found")
        _fields[(owner, number)] = Fields(
            f"{owner}/{number}", project["id"],
            {n["name"]: (n["id"], {o["name"]: o["id"]
                                   for o in n.get("options") or []})
             for n in project["fields"]["nodes"] if n.get("name")})
    return _fields[(owner, number)]


def forget(owner: str, number: int) -> None:
    """Drop the cached fields of project `number` of `owner`: the next
    fields() call reads the live board again."""
    _fields.pop((owner, number), None)


# -- the triage sweep's view --------------------------------------------------


def _gh(run, args: list[str], env: dict[str, str] | None = None) -> str:
    out = run(["gh"] + args, capture_output=True, text=True,
              timeout=GH_TIMEOUT, env=env)
    if out.returncode != 0:
        raise BoardError(f"gh {' '.join(args[:2])} failed: "
                         f"{(out.stderr or '').strip()}")
    return out.stdout


def _set(field: str, value: str) -> str:
    return (f'updateProjectV2ItemFieldValue(input:{{projectId:$p,itemId:$i,'
            f'fieldId:"{field}",value:{{{value}}}}}){{clientMutationId}}')


@dataclass(frozen=True)
class Board:
    fields: Fields
    items: dict[int, Item]   # issue number -> item
    issues: dict[int, dict]  # every issue: number, title, state, stateReason
    run: Callable = subprocess.run

    def _open(self) -> list[int]:
        return sorted(n for n, row in self.issues.items()
                      if row.get("state") == "OPEN")

    def untriaged(self) -> list[int]:
        """Open issues on the board that nobody has scored or promoted."""
        return [n for n in self._open() if n in self.items and (
            self.items[n].status in UNTRIAGED_STATUSES
            or self.items[n].impact is None or self.items[n].effort is None)]

    def off_board(self) -> list[int]:
        """Open issues with no joinable item: the sweep cannot score them."""
        return [n for n in self._open() if n not in self.items]

    def view(self, number: int) -> dict | None:
        item = self.items.get(number)
        return None if item is None else {
            "status": item.status, "impact": item.impact,
            "effort": item.effort, "area": item.area}

    def _mutate(self, item: Item, sets: dict[str, str]) -> None:
        body = " ".join(f"{alias}:{_set(field, value)}"
                        for alias, (field, value) in sets.items())
        _gh(self.run, ["api", "graphql", "-f", f"p={self.fields.id}",
                       "-f", f"i={item.id}", "-f",
                       f"query=mutation($p:ID!,$i:ID!){{{body}}}"],
            env=project_env())

    def score(self, number: int, impact: object, effort: object,
              area: object) -> None:
        """Impact, Effort, Score, Area and Status: Ready in one request.
        Only an untriaged issue is scorable: values a person set stay."""
        if number not in self.untriaged():
            raise ValueError(f"#{number}: not awaiting a score")
        for name, value in (("impact", impact), ("effort", effort)):
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not 1 <= value <= 5:
                raise ValueError(f"#{number}: {name} must be an integer "
                                 f"1..5, got {value!r}")
        f = self.fields
        areas = f.options("Area")
        if area not in areas:
            raise ValueError(f"#{number}: unknown area {area!r}; expected one "
                             f"of {sorted(areas)}")
        self._mutate(self.items[number], {
            "impact": (f.field("Impact"), f"number:{impact}"),
            "effort": (f.field("Effort"), f"number:{effort}"),
            "score": (f.field("Score"), f"number:{round(impact / effort, 1)}"),
            "area": (f.field("Area"), f'singleSelectOptionId:"{areas[area]}"'),
            "status": (f.field("Status"), 'singleSelectOptionId:'
                       f'"{f.option("Status", "Ready")}"')})

    def repair(self) -> list[str]:
        """Closed issues whose card still shows an open status: the board's
        "Item closed" workflow does not always run. One report line each."""
        lines = []
        for number, row in sorted(self.issues.items()):
            item = self.items.get(number)
            if (row.get("state") != "CLOSED" or item is None
                    or item.status not in OPEN_STATUSES):
                continue
            status = ("Wont do" if row.get("stateReason") == "NOT_PLANNED"
                      else "Done")
            try:
                self._mutate(item, {"status": (
                    self.fields.field("Status"), 'singleSelectOptionId:'
                    f'"{self.fields.option("Status", status)}"')})
                lines.append(f"#{number} closed: board status -> {status}")
            except (BoardError, subprocess.SubprocessError) as e:
                lines.append(f"#{number} closed: board status NOT repaired ({e})")
        return lines


def read(repo: str, owner: str, number: int, run=subprocess.run) -> Board:
    """`repo`'s issues and the items of its project, board `number` of
    `owner`."""
    env = project_env()
    f = fields(owner, number, lambda args: _gh(run, args, env))
    items = fetch_items(f.id, lambda args: _gh(run, args, env))
    rows = json.loads(_gh(run, [
        "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000",
        "--json", "number,title,state,stateReason"]))
    return Board(fields=f, items=join(items, rows),
                 issues={r["number"]: r for r in rows}, run=run)
