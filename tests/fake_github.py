"""A stateful fake of the slice of GitHub the board work talks to through
`gh`: Projects v2 boards (fields, options with ids, items, linked
repositories), repository issues and labels. Reruns see earlier writes, so
idempotence is tested as it happens on GitHub. Call it as an `sh` route:
`fake(argv, cwd, env)`."""
from __future__ import annotations

import itertools
import json
import re

STATUSES = ("Inbox", "Ready", "In progress", "Done", "Wont do")
NUMBERS = ("Impact", "Effort", "Score", "Boost")


def _vars(argv: list[str]) -> dict:
    out = {}
    for flag, value in zip(argv, argv[1:]):
        if flag in ("-f", "-F") and "=" in value:
            k, v = value.split("=", 1)
            out[k] = int(v) if flag == "-F" and v.isdigit() else v
    return out


_OPTION = re.compile(r'\{(?:id:"([^"]*)",)?name:("(?:[^"\\]|\\.)*"),'
                     r'color:(\w+),description:("(?:[^"\\]|\\.)*")\}')


class FakeGitHub:
    def __init__(self):
        self._ids = itertools.count(1)
        self.owners = {"jesdi": "U_jesdi"}
        self.boards: dict[tuple[str, int], dict] = {}
        self.issues: dict[str, list[dict]] = {}     # repo -> issues and PRs
        self.labels: dict[str, dict[str, tuple]] = {}
        self.repo_ids: dict[str, str] = {}
        self.fail_after: str | None = None          # an operation to crash after, once
        self.calls: list[str] = []

    # -- setup ------------------------------------------------------------------

    def id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    def board(self, owner="jesdi", number=4, title="Board", fields=None) -> dict:
        b = {"id": self.id("PVT"), "number": number, "owner": owner, "title": title,
             "fields": {}, "items": [], "repos": set()}
        for name, kind in (fields if fields is not None else complete()).items():
            self.field(b, name, kind)
        self.boards[(owner, number)] = b
        return b

    def field(self, b: dict, name: str, kind, options=()) -> dict:
        if isinstance(kind, tuple):          # ("SINGLE_SELECT", options)
            kind, options = kind
        f = {"id": self.id("PVTF"), "name": name, "type": kind,
             "options": [{"id": self.id("OPT"), "name": o, "color": "GRAY",
                          "description": ""} for o in options]}
        b["fields"][name] = f
        return f

    def issue(self, repo: str, number: int, title: str = "", state="OPEN", kind="ISSUE") -> dict:
        self.repo_ids.setdefault(repo, self.id("R"))
        i = {"id": self.id("I"), "number": number, "title": title or f"{repo}#{number}",
             "state": state, "kind": kind, "repo": repo,
             "url": f"https://github.com/{repo}/{'pull' if kind == 'PULL_REQUEST' else 'issues'}/{number}"}
        self.issues.setdefault(repo, []).append(i)
        return i

    def card(self, b: dict, content: dict | None, status: str | None = None,
             redacted=False, **values) -> dict:
        item = {"id": self.id("PVTI"), "content": content and content["id"],
                "draft": content is None, "redacted": redacted,
                "title": content["title"] if content else "draft", "values": {}}
        if status:
            item["values"]["Status"] = self.option_id(b, "Status", status)
        item["values"].update(values)
        b["items"].append(item)
        return item

    def option_id(self, b: dict, field: str, name: str) -> str:
        return next(o["id"] for o in b["fields"][field]["options"] if o["name"] == name)

    # -- reads for assertions --------------------------------------------------

    def status(self, b: dict, content: dict) -> str | None:
        item = self.item_of(b, content)
        value = item["values"].get("Status")
        return next((o["name"] for o in b["fields"]["Status"]["options"] if o["id"] == value), None)

    def item_of(self, b: dict, content: dict) -> dict | None:
        found = [i for i in b["items"] if i["content"] == content["id"]]
        assert len(found) <= 1, f"{content['url']} has {len(found)} cards"
        return found[0] if found else None

    def _content(self, node_id: str) -> dict | None:
        return next((i for issues in self.issues.values() for i in issues
                     if i["id"] == node_id), None)

    def _board_by_id(self, project_id: str) -> dict:
        return next(b for b in self.boards.values() if b["id"] == project_id)

    # -- gh ---------------------------------------------------------------------

    def __call__(self, argv, cwd=None, env=None):
        args = argv[1:] if argv and argv[0] == "gh" else argv
        line = " ".join(args)
        self.calls.append(line)
        if args[:2] == ["label", "list"]:
            repo = args[args.index("--repo") + 1]
            return json.dumps([{"name": n} for n in self.labels.get(repo, {})])
        if args[:2] == ["label", "create"]:
            repo = args[args.index("--repo") + 1]
            have = self.labels.setdefault(repo, {})
            assert args[2] not in have, f"label {args[2]} recreated on {repo}"
            have[args[2]] = (args[args.index("--color") + 1],
                             args[args.index("--description") + 1])
            return ""
        if args[:2] == ["api", "graphql"]:
            out = self._graphql(next(a[6:] for a in args if a.startswith("query=")), _vars(args))
            if self.fail_after and self.fail_after in line:
                self.fail_after = None
                raise RuntimeError("crash after the write")
            return json.dumps([{"data": out}] if "--slurp" in args else {"data": out})
        m = re.fullmatch(r"api repos/([\w.-]+/[\w.-]+)", line)
        if m:
            repo = m.group(1)
            self.repo_ids.setdefault(repo, self.id("R"))
            return json.dumps({"node_id": self.repo_ids[repo], "full_name": repo})
        raise AssertionError(f"FakeGitHub: unexpected gh {line}")

    def _graphql(self, q: str, v: dict) -> dict:
        if "createProjectV2Field" in q:
            b = self._board_by_id(v["p"])
            options = [json.loads(name) for _id, name, _c, _d in _OPTION.findall(q)]
            kind = "SINGLE_SELECT" if "SINGLE_SELECT" in q else "NUMBER"
            self.field(b, v["name"], kind, options)
            return {}
        if "updateProjectV2Field" in q:
            return self._update_options(v["f"], _OPTION.findall(q))
        if "createProjectV2(" in q:
            owner = next(o for o, i in self.owners.items() if i == v["owner"])
            number = max([n for (o, n) in self.boards if o == owner] or [0]) + 1
            b = self.board(owner, number, v["title"], fields={"Status": ("SINGLE_SELECT", ("Todo", "In Progress", "Done"))})
            return {"createProjectV2": {"projectV2": {"number": b["number"]}}}
        if "projectsV2(" in q:
            owner = v["owner"]
            return {"repositoryOwner": {"projectsV2": {"nodes": [
                {"number": b["number"], "title": b["title"]}
                for (o, _n), b in self.boards.items() if o == owner and v["title"] in b["title"]]}}}
        if "linkProjectV2ToRepository" in q:
            self._board_by_id(v["p"])["repos"].add(v["r"])
            return {}
        if "addProjectV2ItemById" in q:
            return self._add(v["p"], v["c"])
        if "updateProjectV2ItemFieldValue" in q:
            b = self._board_by_id(v["p"])
            item = next(i for i in b["items"] if i["id"] == v["i"])
            field = next(f for f in b["fields"].values() if f["id"] == v["f"])
            item["values"][field["name"]] = v["o"]
            return {}
        if "repositories(first" in q:
            b = self._board_by_id(v["p"])
            return {"node": {"repositories": {"nodes": [{"id": r} for r in sorted(b["repos"])]}}}
        if "items(first" in q:
            return self._items(self._board_by_id(v["project"]))
        if "fields(first" in q:
            return self._fields(v["owner"], v["number"])
        if "issues(first" in q:
            return self._issues(f"{v['owner']}/{v['name']}")
        if "repositoryOwner(login: $owner) { id" in q:
            return {"repositoryOwner": {"id": self.owners[v["owner"]]}}
        raise AssertionError(f"FakeGitHub: unexpected graphql {q[:80]}")

    def _fields(self, owner: str, number: int) -> dict:
        b = self.boards.get((owner, number))
        if b is None:
            return {"repositoryOwner": None}
        return {"repositoryOwner": {"projectV2": {
            "id": b["id"], "url": f"https://github.com/users/{owner}/projects/{number}",
            "fields": {"nodes": [
                {"id": f["id"], "name": f["name"], "dataType": f["type"],
                 **({"options": [dict(o) for o in f["options"]]} if f["type"] == "SINGLE_SELECT" else {})}
                for f in b["fields"].values()]}}}}

    def _value(self, b: dict, item: dict, field: str):
        f = b["fields"].get(field)
        value = item["values"].get(field)
        if f is None or value is None:
            return None
        if f["type"] == "SINGLE_SELECT":
            name = next((o["name"] for o in f["options"] if o["id"] == value), None)
            return name and {"name": name}
        return {"number": value}

    def _items(self, b: dict) -> dict:
        nodes = []
        for item in b["items"]:
            content = self._content(item["content"]) if item["content"] else None
            node = {"id": item["id"],
                    "type": "DRAFT_ISSUE" if item["draft"] else "REDACTED" if item["redacted"]
                    else content["kind"],
                    "content": {} if item["redacted"] or content is None else (
                        {"number": content["number"], "title": content["title"],
                         "url": content["url"], "repository": {"nameWithOwner": content["repo"]}}
                        if content["kind"] == "ISSUE" else
                        {"url": content["url"], "repository": {"nameWithOwner": content["repo"]}}),
                    "title": {"text": item["title"]}}
            for alias, field in (("status", "Status"), ("area", "Area"), ("impact", "Impact"),
                                 ("effort", "Effort"), ("boost", "Boost")):
                node[alias] = self._value(b, item, field)
            nodes.append(node)
        return {"node": {"items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                   "nodes": nodes}}}

    def _issues(self, repo: str) -> dict:
        return {"repository": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"id": i["id"], "number": i["number"], "title": i["title"],
                       "url": i["url"], "body": "", "state": i["state"],
                       "labels": {"nodes": []}}
                      for i in self.issues.get(repo, []) if i["kind"] == "ISSUE"]}}}

    def _add(self, project_id: str, content_id: str) -> dict:
        b = self._board_by_id(project_id)
        content = self._content(content_id)
        item = next((i for i in b["items"] if i["content"] == content_id), None)
        if item is None:
            item = self.card(b, content)
        return {"addProjectV2ItemById": {"item": {
            "id": item["id"], "status": self._value(b, item, "Status")}}}

    def _update_options(self, field_id: str, sent: list[tuple]) -> dict:
        """Options resent with their id keep it and the values set to them;
        any other option gets a new id, and values set to a dropped id are
        cleared, as GitHub does (#213)."""
        b, f = next((b, f) for b in self.boards.values() for f in b["fields"].values()
                    if f["id"] == field_id)
        f["options"] = [{"id": oid or self.id("OPT"), "name": json.loads(name), "color": color,
                         "description": json.loads(desc)} for oid, name, color, desc in sent]
        kept = {o["id"] for o in f["options"]}
        for item in b["items"]:
            if item["values"].get(f["name"]) not in kept:
                item["values"].pop(f["name"], None)
        return {}


def complete() -> dict:
    return {"Status": ("SINGLE_SELECT", STATUSES), "Area": ("SINGLE_SELECT", ("feature", "bug")),
            **{n: "NUMBER" for n in NUMBERS}, "Title": "TITLE"}
