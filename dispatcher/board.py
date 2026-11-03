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
import dataclasses
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
      content {
        ... on Issue { number title url repository { nameWithOwner } }
        ... on PullRequest { url repository { nameWithOwner } } }
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
    id url
    fields(first: 100) { nodes {
      ... on ProjectV2FieldCommon { id name dataType }
      ... on ProjectV2SingleSelectField { options { id name color description } } } }
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
    types: dict[str, str] = dataclasses.field(default_factory=dict)  # field -> dataType
    specs: dict[str, list[dict]] = dataclasses.field(default_factory=dict)  # field -> its options in full
    url: str = ""

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
        nodes = [n for n in project["fields"]["nodes"] if n.get("name")]
        _fields[(owner, number)] = Fields(
            f"{owner}/{number}", project["id"],
            {n["name"]: (n["id"], {o["name"]: o["id"]
                                   for o in n.get("options") or []})
             for n in nodes},
            types={n["name"]: n.get("dataType", "") for n in nodes},
            specs={n["name"]: n["options"] for n in nodes if "options" in n},
            url=project.get("url", ""))
    return _fields[(owner, number)]


def forget(owner: str, number: int) -> None:
    """Drop the cached fields of project `number` of `owner`: the next
    fields() call reads the live board again."""
    _fields.pop((owner, number), None)


# -- preparing a board (#230) ---------------------------------------------------

STATUSES = ("Inbox", "Ready", "In progress", "Done", "Wont do")
AREAS = ("feature", "bug", "infra", "docs", "research")
REQUIRED = {"Status": "SINGLE_SELECT", "Impact": "NUMBER", "Effort": "NUMBER",
            "Score": "NUMBER", "Boost": "NUMBER", "Area": "SINGLE_SELECT"}
_NEW_OPTIONS = {"Status": STATUSES, "Area": AREAS}


@dataclass(frozen=True)
class Gaps:
    """What a board lacks of the fixed structure: additions only."""
    fields: tuple[str, ...] = ()     # required fields it lacks
    statuses: tuple[str, ...] = ()   # Status options it lacks

    def lines(self) -> list[str]:
        kind = {"NUMBER": "number", "SINGLE_SELECT": "single select"}
        return [*(f"add {kind[REQUIRED[n]]} field {n}" for n in self.fields),
                *(f"add Status option {s}" for s in self.statuses)]


def gaps(f: Fields) -> Gaps:
    """`f`'s gaps; a BoardError naming every required field of another type,
    which is never replaced or guessed at (S10)."""
    clash = [f"{n} is {f.types[n].lower().replace('_', ' ')}, not "
             f"{REQUIRED[n].lower().replace('_', ' ')}"
             for n in REQUIRED if n in f.types and f.types[n] != REQUIRED[n]]
    if clash:
        raise BoardError(f"project {f.project}: field {'; '.join(clash)}: rename or "
                         "delete it on the board, or choose another board")
    absent = tuple(n for n in REQUIRED if n not in f.types)
    statuses = () if "Status" in absent else tuple(
        s for s in STATUSES if s not in f.options("Status"))
    return Gaps(absent, statuses)


def _option(o: dict) -> str:
    """One ProjectV2SingleSelectFieldOptionInput literal; with its id when it
    has one, so the values set to it survive (#213)."""
    oid = f'id:{json.dumps(o["id"])},' if o.get("id") else ""
    return (f'{{{oid}name:{json.dumps(o["name"])},color:{o.get("color") or "GRAY"},'
            f'description:{json.dumps(o.get("description") or "")}}}')


def _options(options) -> str:
    return "[" + ",".join(_option(o) for o in options) + "]"


def prepare(f: Fields, g: Gaps, gh: Callable[[list[str]], str], fresh: bool = False) -> None:
    """Add what `g` names to board `f`. Status options are appended with
    every existing option resent by id: an update replaces the whole list.
    On a `fresh` board, one onboarding created, Status holds exactly
    STATUSES instead of GitHub's defaults (Todo, In Progress, Done)."""
    for name in g.fields:
        if REQUIRED[name] == "NUMBER":
            body = "dataType:NUMBER"
        else:
            body = "dataType:SINGLE_SELECT,singleSelectOptions:" + _options(
                {"name": o} for o in _NEW_OPTIONS[name])
        gh(["api", "graphql", "-f", f"p={f.id}", "-f", f"name={name}", "-f",
            "query=mutation($p:ID!,$name:String!){createProjectV2Field(input:"
            f"{{projectId:$p,name:$name,{body}}}){{clientMutationId}}}}"])
    if g.statuses:
        options = [*f.specs["Status"], *({"name": s} for s in g.statuses)]
        if fresh:
            options = [next((o for o in options if o["name"] == s), {"name": s}) for s in STATUSES]
        gh(["api", "graphql", "-f", f"f={f.field('Status')}", "-f",
            "query=mutation($f:ID!){updateProjectV2Field(input:{fieldId:$f,"
            f"singleSelectOptions:{_options(options)}}}){{clientMutationId}}}}"])


def foreign(items: list[Item], members: set[str], titles: set[str]) -> tuple[list[str], int]:
    """The cards of issues and PRs outside `members` (lowercased owner/name),
    and how many cards the board token can't read whose title no member
    issue carries. Drafts belong to nobody (#230 D4)."""
    outside, unreadable = [], 0
    for item in items:
        if item.kind == "DRAFT_ISSUE":
            continue
        if item.repository:
            if item.repository.lower() not in members:
                outside.append(item.url or item.title)
        elif item.title not in titles:
            unreadable += 1
    return outside, unreadable


def find(owner: str, title: str, gh: Callable[[list[str]], str]) -> list[int]:
    """The numbers of `owner`'s boards titled exactly `title`."""
    out = gh(["api", "graphql", "-f", f"owner={owner}", "-f", f"title={title}", "-f",
              "query=query($owner:String!,$title:String!){repositoryOwner(login:$owner){"
              "... on ProjectV2Owner { projectsV2(first:100,query:$title){nodes{number title}}}}}"])
    nodes = ((json.loads(out)["data"].get("repositoryOwner") or {})
             .get("projectsV2") or {}).get("nodes") or []
    return [n["number"] for n in nodes if n["title"] == title]


def create(owner: str, title: str, gh: Callable[[list[str]], str]) -> int:
    """A new board of `owner`; its number."""
    owner_id = json.loads(gh(["api", "graphql", "-f", f"owner={owner}", "-f",
                              "query=query($owner:String!){repositoryOwner(login: $owner) { id } }"])
                          )["data"]["repositoryOwner"]["id"]
    out = gh(["api", "graphql", "-f", f"owner={owner_id}", "-f", f"title={title}", "-f",
              "query=mutation($owner:ID!,$title:String!){createProjectV2(input:"
              "{ownerId:$owner,title:$title}){projectV2{number}}}"])
    return json.loads(out)["data"]["createProjectV2"]["projectV2"]["number"]


def link(f: Fields, repository_ids: list[str], gh: Callable[[list[str]], str]) -> None:
    """Link board `f` to every repository (node id) not linked yet."""
    out = gh(["api", "graphql", "-f", f"p={f.id}", "-f",
              "query=query($p:ID!){node(id:$p){... on ProjectV2 {"
              "repositories(first:100){nodes{id}}}}}"])
    linked = {n["id"] for n in json.loads(out)["data"]["node"]["repositories"]["nodes"]}
    for rid in repository_ids:
        if rid not in linked:
            gh(["api", "graphql", "-f", f"p={f.id}", "-f", f"r={rid}", "-f",
                "query=mutation($p:ID!,$r:ID!){linkProjectV2ToRepository(input:"
                "{projectId:$p,repositoryId:$r}){clientMutationId}}"])


_REPO_ISSUES_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) { issues(first: 100, after: $endCursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id number title url body state labels(first: 100) { nodes { name } } }
  } }
}
"""


def repo_issues(repo: str, gh: Callable[[list[str]], str]) -> list[dict]:
    """Every issue of `repo`, open and closed, as ranking.rank reads them,
    with its node id. GraphQL pages, so no count cap. `gh` reads the repo."""
    owner, name = repo.split("/", 1)
    pages = json.loads(gh(["api", "graphql", "--paginate", "--slurp",
                           "-f", f"query={_REPO_ISSUES_QUERY}",
                           "-f", f"owner={owner}", "-f", f"name={name}"]))
    return [{**node, "repository": repo,
             "labels": [label["name"] for label in node["labels"]["nodes"]]}
            for page in pages for node in page["data"]["repository"]["issues"]["nodes"]]


def add_missing(f: Fields, items: list[Item], issues: list[dict],
                gh: Callable[[list[str]], str]) -> list[str]:
    """Add a card for every open issue in `issues` with none on board `f`;
    the urls added. A card the add finds without a Status gets Inbox, so
    triage on an existing card is never reset (#230 D6, D7). Adding is by
    node id, so equal numbers in two repositories never collide (S57)."""
    from dispatcher import ranking
    on_board = ranking.on_board(items, issues)
    added = []
    for issue in issues:
        if issue["state"] != "OPEN" or (issue["repository"].lower(), issue["number"]) in on_board:
            continue
        out = gh(["api", "graphql", "-f", f"p={f.id}", "-f", f"c={issue['id']}", "-f",
                  "query=mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:"
                  "{projectId:$p,contentId:$c}){item{id status: fieldValueByName("
                  'name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }}}}'])
        item = json.loads(out)["data"]["addProjectV2ItemById"]["item"]
        if not item.get("status"):
            gh(["api", "graphql", "-f", f"p={f.id}", "-f", f"i={item['id']}",
                "-f", f"f={f.field('Status')}", "-f", f"o={f.option('Status', 'Inbox')}", "-f",
                "query=mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
                "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}})"
                "{clientMutationId}}"])
        added.append(issue["url"])
    return added


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
