"""Operator-terminal onboarding CLI (`agent-ops project ...`).

Proposes an onboarding from what the box can inspect, lets the operator edit
it, and hands the confirmed revision to the box. Only `box.confirm` and
`box.resume` change anything; everything before them is a read.
"""
import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy

from dispatcher.config import normalize_repo
from project_control.board_validation import _check_board

_COMMANDS = ("setup_cmd", "verify_cmd", "gate_cmd")
_EDITABLE = _COMMANDS + ("compose_services",)
_LABEL = "auto"
_BOARD_HELP = "edit board=<owner>/<number> or edit board=create:<owner>/<title>"


class SshBox:
    """The box over SSH: `ssh $AGENT_OPS_BOX agent-ops-box <verb> [json]`."""

    def __init__(self, host):
        self.host = host

    def _call(self, verb, payload):
        done = subprocess.run(["ssh", self.host, "agent-ops-box", verb],
                              input=json.dumps(payload), capture_output=True,
                              text=True, check=True)
        return json.loads(done.stdout or "null")

    def projects(self):
        return self._call("projects", None)

    def inspect(self, repo):
        return self._call("inspect", repo)

    def board_state(self, ref):
        return self._call("board_state", ref)

    def confirm(self, plan):
        return self._call("confirm", plan)

    def resume(self, name):
        return self._call("resume", name)


def cli():
    sys.exit(main(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                  box=SshBox(os.environ.get("AGENT_OPS_BOX", "agent-ops"))))


def _build(name, repos, found):
    boards = [f["board"] for f in found.values() if f.get("board")]
    return {
        "name": name,
        "repositories": [{"repo": r, **{k: found[r].get(k) for k in _COMMANDS},
                         "compose_services": list(found[r].get("compose_services", []))}
                        for r in repos],
        "board": boards[0] if boards else None,
        "models": "inherit",
        "access": [f"{r}: clone, push branches, open pull requests, board access" for r in repos],
        "secret_refs": sorted({s for f in found.values() for s in f.get("secret_refs", [])}),
        "write_tests": [f"{r}: temporary branch push, temporary pull request, then delete both"
                        for r in repos]
                       + (["board: temporary item update, then restore it"] if boards else []),
    }

def _prepare(plan, found, box):
    """Fill board_prep and backlog from the selected board; return the reasons it is blocked."""
    board, state = plan["board"], None
    plan["board_prep"] = {"fields": [], "options": [], "labels": {}}
    plan["backlog"] = {"add": {}, "future": "reconcile", "status": None, "labels": []}
    if board is not None and board.get("create"):
        state = {"fields": [], "cards": []}
    elif board is not None:
        inspected = next((f for f in found.values() if f.get("board") == board and "board_state" in f), None)
        if inspected:
            state = inspected["board_state"]
            if state is None:
                return [f"board {_board_label(board)} was not found or is not accessible"]
        elif hasattr(box, "board_state"):
            state = box.board_state(board)
            if state is None:
                return [f"board {_board_label(board)} was not found or is not accessible"]
    members = [r["repo"] for r in plan["repositories"]]
    if state is None:
        return []
    problems, plan["board_prep"]["fields"], plan["board_prep"]["options"] = _check_board(state, members)
    if problems:
        plan["board_prep"]["fields"] = plan["board_prep"]["options"] = []
        return problems
    carded = {(normalize_repo(c["repo"]), c["number"]) for c in state["cards"]}
    for r in members:
        info = found[r]
        if "labels" in info and _LABEL not in info["labels"]:
            plan["board_prep"]["labels"][r] = [_LABEL]
        todo = [i["number"] for i in info.get("open_issues", []) if (r, i["number"]) not in carded]
        if todo:
            plan["backlog"]["add"][r] = todo
    return []


def _revision(plan):
    body = {k: v for k, v in plan.items() if k != "revision"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]


def _show(value):
    """Box-supplied text, escaped when it holds control characters that could spoof the prompt."""
    text = str(value)
    return text if text.isprintable() else repr(text)


def _changes(plan):
    prep = plan["board_prep"]
    return ([f"field {f['name']} ({f['type']})"
             + (f": {', '.join(f['options'])}" if f.get("options") else "")
             for f in prep["fields"]]
            + [f"option {o['field']}: {o['name']}" for o in prep["options"]]
            + [f"label {r}: {', '.join(ls)}" for r, ls in prep["labels"].items()])


def _render(plan, out):
    plan["revision"] = _revision(plan)
    board = plan["board"]
    print("Configuration:", file=out)
    print(f"  project: {plan['name']}", file=out)
    print(f"  board: {_board_label(board)}", file=out)
    print(f"  models: {plan['models']}", file=out)
    missing = []
    for r in plan["repositories"]:
        print(f"  {r['repo']}:", file=out)
        for k in _COMMANDS:
            print(f"    {k}: {_show(r[k]) if r[k] is not None else '(missing)'}", file=out)
            if r[k] is None:
                missing.append(f"{r['repo']}: {k}")
    for title, lines in (
            ("Access:", plan["access"]),
            ("Services:", [f"{r['repo']}: {', '.join(map(_show, r['compose_services'])) or 'none declared'}"
                           for r in plan["repositories"]]),
            ("Secret references:", list(map(_show, plan["secret_refs"])) or ["none declared"]),
            ("Write tests:", plan["write_tests"]),
            ("Board changes:", _changes(plan)),
            ("Backlog:", [f"{r}: add {', '.join(f'#{n}' for n in ns)}"
                          for r, ns in plan["backlog"]["add"].items()]
                         + (["new issues are added by the dispatcher's periodic reconcile"]
                            if plan["backlog"]["add"] else [])),
            ("Missing:", missing + (["board"] if board is None else []))):
        if lines:
            print(f"\n{title}", *(f"  {line}" for line in lines), sep="\n", file=out)
    print(f"\nRevision: {plan['revision']}", file=out)
    print(f"Board choice: {_BOARD_HELP}", file=out)


def _board_label(board):
    if board is None:
        return "(missing)"
    if board.get("create"):
        return f"create {_show(board['owner'])}/{_show(board['title'])}"
    return f"{_show(board['owner'])}/{_show(board['number'])}"


def _edit_board(plan, value):
    """Describe a board selection or creation; all remote writes wait for confirm.

    Existing boards retain the inspected owner/number contract. New boards use
    {owner, create: true, title}; the box resolves identity, access and fields
    and validates project membership before applying either choice.
    """
    create = value.startswith("create:")
    choice = value.removeprefix("create:") if create else value
    owner, separator, identity = choice.partition("/")
    if not separator or not owner or not identity or not choice.isprintable():
        return f"Invalid board choice: {_BOARD_HELP}"
    if any(c.isspace() for c in owner):
        return f"Invalid board owner: {_BOARD_HELP}"
    if create:
        board = {"owner": owner, "create": True, "title": identity}
    else:
        if not identity.isascii() or not identity.isdigit() or int(identity) < 1:
            return f"Invalid board number: {_BOARD_HELP}"
        board = {"owner": owner, "number": int(identity)}
    plan["board"] = board
    board_test = "board: temporary item update, then restore it"
    if board_test not in plan["write_tests"]:
        plan["write_tests"].append(board_test)
    return None


def _edit(plan, spec):
    """Apply an operator edit; return an error message or None."""
    target, _, value = spec.partition("=")
    if target == "board":
        return _edit_board(plan, value)
    if target in ("models", f"{plan['name']}.models"):
        plan["models"] = value
        return None
    repo, _, field = target.rpartition(".")
    entry = next((r for r in plan["repositories"]
                  if r["repo"] == normalize_repo(repo)), None)
    if entry is None or field not in _EDITABLE:
        return f"cannot edit {target!r}: use <repo>.<{'|'.join(_EDITABLE)}>=<value>"
    entry[field] = ([s.strip() for s in value.split(",") if s.strip()]
                    if field == "compose_services" else value)
    return None


def _ask(prompt, stdin, out):
    print(prompt, end="", file=out, flush=True)
    return stdin.readline().strip()


def _add(args, stdin, out, err, box):
    name = args[0] if args else _ask("Project name: ", stdin, out)
    repos = args[1:] or _ask("Repositories (space separated): ", stdin, out).split()
    if not name or not repos:
        print("project add needs a name and at least one repository", file=err)
        return 2
    repos = [normalize_repo(r) for r in repos]
    registered = box.projects()
    owners = {normalize_repo(r): p for p, rs in registered.items() for r in rs}
    taken = {r: owners[r] for r in repos if owners.get(r, name) != name}
    if taken:
        for r, owner in taken.items():
            print(f"{r} is already registered in project {owner!r}; "
                  f"run `agent-ops project add {owner}` to resume it", file=err)
        return 1
    if name in registered and {normalize_repo(r) for r in registered[name]} == set(repos):
        print(f"Project {name!r} already exists; its pending onboarding can be resumed", file=out)
        if _ask("Resume this onboarding? [yes / no] ", stdin, out) != "yes":
            print("Declined; nothing was changed", file=out)
            return 0
        print(f"Resuming onboarding of {name!r}", file=out)
        box.resume(name)
        return 0
    found = {r: box.inspect(r) for r in repos}
    plan = _build(name, repos, found)
    blocked = _prepare(plan, found, box)
    while True:
        _render(plan, out)
        for problem in blocked:
            print(f"Board blocked: {problem}; choose or create another: {_BOARD_HELP}", file=err)
        answer = _ask("Apply this revision? [yes / edit <repo>.<field>=<value> / no] ",
                      stdin, out)
        if answer == "yes":
            if blocked:
                continue
            if plan["board"] is None:
                print(f"Choose a board before confirming: {_BOARD_HELP}", file=err)
                continue
            box.confirm(plan)
            print(f"Confirmed revision {plan['revision']}", file=out)
            return 0
        if not answer.startswith("edit "):
            print("Declined; nothing was changed", file=out)
            return 0
        previous_plan, was_blocked = deepcopy(plan), blocked
        problem = _edit(plan, answer[5:].strip())
        if not problem and plan["board"] != previous_plan["board"]:
            blocked = _prepare(plan, found, box)
            if blocked and blocked[0].endswith("is not accessible"):
                plan, blocked = previous_plan, was_blocked
                problem = "board not found or not accessible"
        if problem:
            print(problem, file=err)


def main(argv, *, stdin, stdout, stderr, box) -> int:
    if list(argv[:2]) == ["project", "add"]:
        return _add(list(argv[2:]), stdin, stdout, stderr, box)
    print("usage: agent-ops project add [name] [repository ...]", file=stderr)
    return 2
