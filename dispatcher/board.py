"""The Projects-v2 board, read and written with narrow GraphQL.

`gh project item-list` asks for every field and several nested connections
per item: on a ~100-item board one call costs on the order of 100 of the
5000 GraphQL points GitHub allows per user per hour, and the dispatcher used
to make it on every status write. The query here costs about 1 point per 100
items.

Two tokens are in play on the box: the stored fine-grained auth reads the
repo, and the classic project-scope token (GH_PROJECT_TOKEN) reads and writes
the board. The project token cannot expand a private repo's issues, so an
item's issue number can be absent. Such cards are unavailable until GitHub
exposes their repository-qualified identity."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dispatcher.config import normalize_repo

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
      content { ... on Issue { number repository { nameWithOwner } } }
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


def project_env(board: str | None = None, credentials=None) -> dict[str, str] | None:
    """User-owned Projects v2 are invisible to fine-grained PATs, so board
    calls run with GH_TOKEN set to the classic project-scope token
    (GH_PROJECT_TOKEN); every other gh call keeps the stored auth.

    With a credential service the token is the trusted board credential for
    `board` ("owner/number"); an unregistered board raises CredentialDenied
    and never falls back to GH_PROJECT_TOKEN."""
    if credentials is not None:
        from dispatcher.execution_credentials import TRUSTED
        token = credentials.acquire(TRUSTED, board, "board").token
        return {**os.environ, "GH_TOKEN": token}
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
    repo: str | None = None  # "owner/name"; None when GitHub doesn't expose it


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
            content = node.get("content") or {}
            items.append(Item(
                id=node["id"],
                number=content.get("number"),
                title=_value(node, "title") or "",
                status=_value(node, "status"),
                impact=_value(node, "impact"),
                effort=_value(node, "effort"),
                area=_value(node, "area"),
                repo=(content.get("repository") or {}).get("nameWithOwner")))
        if not page["pageInfo"]["hasNextPage"]:
            return items
        cursor = page["pageInfo"]["endCursor"]


def join(items: list[Item], issues: list[dict], repo: str) -> dict[int, Item]:
    """Join only exposed repository-qualified issue identities.

    Titles cannot establish which repository owns a redacted card, even
    when a title occurs only once in this repository's issue list.
    """
    numbers = {row["number"] for row in issues}
    norm_repo = normalize_repo(repo)
    return {item.number: item for item in items
            if item.repo is not None and normalize_repo(item.repo) == norm_repo
            and item.number in numbers}


# -- the triage sweep's view --------------------------------------------------


@dataclass(frozen=True)
class Schema:
    """Field and option ids from clone metadata or the registered board's API."""
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
    # Trusted authorization of this board ("owner/number" from the dispatcher
    # configuration, never from the clone): a mutation of any other board is
    # refused before gh runs.
    credentials: object = None
    key: str | None = None
    # Node id the registered board `key` resolves to, set by read(); a
    # credentialed mutation of any other project is refused.
    bound: str | None = None
    repo: str | None = None

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
        env = project_env(self.key, self.credentials)
        if self.credentials is not None and self.bound != self.schema.project_id:
            raise BoardError(f"project {self.schema.project_id!r} is not the "
                             f"registered board {self.key!r}")
        body = " ".join(f"{alias}:{_set(field, value)}"
                        for alias, (field, value) in sets.items())
        _gh(self.run, ["api", "graphql", "-f", f"p={self.schema.project_id}",
                       "-f", f"i={item.id}", "-f",
                       f"query=mutation($p:ID!,$i:ID!){{{body}}}"],
            env=env)

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

    def add_missing(self) -> list[str]:
        """Add each open issue without a card, once; existing cards, statuses
        and labels are never touched, and a new card has no status. Adding an
        issue already on the board returns its card, so a repeat is harmless."""
        if self.repo is None:
            return []
        lines = []
        for number in self.off_board():
            try:
                env = project_env(self.key, self.credentials)
                if self.credentials is not None and self.bound != self.schema.project_id:
                    raise BoardError(f"project {self.schema.project_id!r} is not "
                                     f"the registered board {self.key!r}")
                node = json.loads(_gh(self.run, ["issue", "view", str(number), "--repo",
                                                 self.repo, "--json", "id"]))["id"]
                _gh(self.run, ["api", "graphql", "-f", f"p={self.schema.project_id}",
                               "-f", f"c={node}", "-f",
                               "query=mutation($p:ID!,$c:ID!){addProjectV2ItemById("
                               "input:{projectId:$p,contentId:$c}){item{id}}}"], env=env)
                lines.append(f"#{number} added to the board")
            except (BoardError, subprocess.SubprocessError, ValueError, KeyError) as e:
                lines.append(f"#{number} NOT added to the board ({e})")
        return lines

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


_OWNER_PROJECT_QUERY = """
query($login: String!, $number: Int!) {
  repositoryOwner(login: $login) { ... on ProjectV2Owner {
    projectV2(number: $number) { id } } }
}
"""


def resolve_project_id(key: str, gh: Callable[[list[str]], str]) -> str:
    """The node id of the registered board "owner/number", asked of GitHub
    with the trusted credential, so clone metadata cannot redirect a board
    call to another project."""
    owner, _, number = key.rpartition("/")
    try:
        data = json.loads(gh(["api", "graphql", "-f", f"query={_OWNER_PROJECT_QUERY}",
                              "-f", f"login={owner}", "-F", f"number={int(number)}"]))
        return data["data"]["repositoryOwner"]["projectV2"]["id"]
    except (ValueError, KeyError, TypeError) as e:
        raise BoardError(f"cannot resolve registered board {key!r}") from e


def registered_schema(key: str, gh: Callable[[list[str]], str]) -> Schema:
    """Discover mappings from the configured board when the clone has no metadata."""
    project_id = resolve_project_id(key, gh)
    owner, _, number = key.rpartition("/")
    data = json.loads(gh(["project", "field-list", number, "--owner", owner,
                          "--format", "json", "--limit", "1000"]))
    rows = data["fields"]
    if data.get("totalCount", len(rows)) != len(rows):
        raise BoardError("board field listing is incomplete")
    fields = {}
    for name in ("Status", "Impact", "Effort", "Score", "Area"):
        matches = [f for f in rows if f["name"] == name]
        if len(matches) != 1:
            raise BoardError(f"required board field {name!r} missing or ambiguous")
        fields[name] = matches[0]
    return Schema(project_id, fields["Status"]["id"], fields["Impact"]["id"],
                  fields["Effort"]["id"], fields["Score"]["id"], fields["Area"]["id"],
                  {o["name"]: o["id"] for o in fields["Status"]["options"]},
                  {o["name"]: o["id"] for o in fields["Area"]["options"]})


def open_issues(repo: str, run) -> list[dict]:
    """Complete open inventory, independent of the bounded closed-card repair list.

    REST issues includes pull requests; those are never imported as issues.
    Repository reads use repository auth rather than the trusted board token.
    """
    pages = json.loads(_gh(run, ["api", f"repos/{normalize_repo(repo)}/issues?state=open&per_page=100",
                                "--paginate", "--slurp"]))
    return [{"number": row["number"], "title": row["title"], "state": "OPEN"}
            for page in pages for row in page if "pull_request" not in row]


def read(repo: str, clone_path: str, run=subprocess.run, credentials=None,
         key: str | None = None) -> Board | None:
    schema = load_schema(clone_path)
    if schema is None and key is None:
        return None
    env = project_env(key, credentials)
    bound = None
    if key is not None:
        bound = resolve_project_id(key, lambda args: _gh(run, args, env))
        if schema is None or schema.project_id != bound:
            schema = registered_schema(key, lambda args: _gh(run, args, env))
            if schema.project_id != bound:
                raise BoardError("registered board identity changed during discovery")
    items = fetch_items(schema.project_id, lambda args: _gh(run, args, env))
    rows = json.loads(_gh(run, [
        "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000",
        "--json", "number,title,state,stateReason"]))
    rows.extend(open_issues(repo, run))
    return Board(schema=schema, items=join(items, rows, repo),
                 issues={r["number"]: r for r in rows}, run=run,
                 credentials=credentials, key=key, bound=bound, repo=repo)
