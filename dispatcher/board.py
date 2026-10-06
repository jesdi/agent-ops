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
from pathlib import Path
from typing import Callable

GH_TIMEOUT = 120
META_PATH = Path(".backlog") / "project-meta.json"
UNTRIAGED_STATUSES = frozenset({None, "Inbox"})
OPEN_STATUSES = frozenset({None, "Inbox", "Ready", "In progress"})

_ITEMS_QUERY = """
query($project: ID!, $cursor: String) {
  node(id: $project) { ... on ProjectV2 { items(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      content { ... on Issue { number } }
      title: fieldValueByName(name: "Title") { ... on ProjectV2ItemFieldTextValue { text } }
      status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      area: fieldValueByName(name: "Area") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      impact: fieldValueByName(name: "Impact") { ... on ProjectV2ItemFieldNumberValue { number } }
      effort: fieldValueByName(name: "Effort") { ... on ProjectV2ItemFieldNumberValue { number } }
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
    title: str
    status: str | None
    impact: float | None
    effort: float | None
    area: str | None


def _value(node: dict, alias: str):
    return next(iter((node.get(alias) or {}).values()), None)


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
        for node in page["nodes"]:
            items.append(Item(
                id=node["id"],
                number=(node.get("content") or {}).get("number"),
                title=_value(node, "title") or "",
                status=_value(node, "status"),
                impact=_value(node, "impact"),
                effort=_value(node, "effort"),
                area=_value(node, "area")))
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


# -- the triage sweep's view --------------------------------------------------


@dataclass(frozen=True)
class Schema:
    """Field and option ids from the target clone's committed
    `.backlog/project-meta.json` (written by the backlog skill's `setup`)."""
    project_id: str
    status_field: str
    impact_field: str
    effort_field: str
    score_field: str
    area_field: str
    statuses: dict[str, str]
    areas: dict[str, str]


def load_schema(clone_path: str) -> Schema | None:
    """None when the clone has no board pointer, or one without the scoring
    fields: such a repo gets labels only, exactly as before."""
    try:
        meta = json.loads((Path(clone_path) / META_PATH).read_text())
        fields = meta["fields"]
        return Schema(
            project_id=meta["projectId"],
            status_field=fields["Status"]["id"],
            impact_field=fields["Impact"]["id"],
            effort_field=fields["Effort"]["id"],
            score_field=fields["Score"]["id"],
            area_field=fields["Area"]["id"],
            statuses=dict(fields["Status"]["options"]),
            areas=dict(fields["Area"]["options"]))
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


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
    schema: Schema
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
        _gh(self.run, ["api", "graphql", "-f", f"p={self.schema.project_id}",
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
        if area not in self.schema.areas:
            raise ValueError(f"#{number}: unknown area {area!r}; expected one "
                             f"of {sorted(self.schema.areas)}")
        s = self.schema
        self._mutate(self.items[number], {
            "impact": (s.impact_field, f"number:{impact}"),
            "effort": (s.effort_field, f"number:{effort}"),
            "score": (s.score_field, f"number:{round(impact / effort, 1)}"),
            "area": (s.area_field,
                     f'singleSelectOptionId:"{s.areas[area]}"'),
            "status": (s.status_field,
                       f'singleSelectOptionId:"{s.statuses["Ready"]}"')})

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
            option = self.schema.statuses.get(status)
            if not option:
                continue
            try:
                self._mutate(item, {"status": (
                    self.schema.status_field,
                    f'singleSelectOptionId:"{option}"')})
                lines.append(f"#{number} closed: board status -> {status}")
            except (BoardError, subprocess.SubprocessError) as e:
                lines.append(f"#{number} closed: board status NOT repaired ({e})")
        return lines


def read(repo: str, clone_path: str, run=subprocess.run) -> Board | None:
    schema = load_schema(clone_path)
    if schema is None:
        return None
    env = project_env()
    items = fetch_items(schema.project_id, lambda args: _gh(run, args, env))
    rows = json.loads(_gh(run, [
        "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000",
        "--json", "number,title,state,stateReason"]))
    return Board(schema=schema, items=join(items, rows),
                 issues={r["number"]: r for r in rows}, run=run)
