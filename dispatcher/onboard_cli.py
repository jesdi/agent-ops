"""`agent-ops project add <name> <repo>... --board owner/number`: the
terminal front end of the onboarding engine (dispatcher.onboard). Run on the
box as `agent`: it asks for each repository's commands (piped stdin works
too), writes an onboarding intent, starts agent-ops-onboard (the call returns
when the oneshot run ends) and prints the record."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path

import yaml

from dispatcher import config, onboard

USER = "agent"
KICK = ["systemctl", "--user", "start", "agent-ops-onboard.service"]


def _slug(value: str) -> str:
    if not config._SLUG.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r}: a project name is a slug of a-z, 0-9 and -")
    return value


def _board(value: str) -> dict:
    owner, _, number = value.partition("/")
    if not owner or not number.isdigit():
        raise argparse.ArgumentTypeError(f"{value!r}: expected owner/number, e.g. jesdi/4")
    return {"owner": owner, "number": int(number)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-ops")
    add = parser.add_subparsers(dest="noun", required=True).add_parser(
        "project").add_subparsers(dest="verb", required=True).add_parser(
        "add", help="onboard a project: it ends merged, deployed, visible and paused")
    add.add_argument("name", type=_slug)
    add.add_argument("repos", nargs="+", metavar="repo")
    add.add_argument("--board", type=_board, required=True, help="owner/number")
    add.add_argument("--config", default=os.environ.get(
        "AGENT_OPS_CONFIG", str(Path.home() / "agent-ops-state" / "targets.yaml")))
    return parser


def main(argv: list[str] | None = None, sh: onboard.Sh = onboard.sh) -> int:
    args = _parser().parse_args(argv)
    if getpass.getuser() != USER:
        print(f"run agent-ops as {USER} (sudo -iu {USER})", file=sys.stderr)
        return 2
    state_dir = config.load_config(args.config).state_dir
    rec = onboard.load_record(state_dir, args.name)
    if rec and rec["status"] == "complete":
        print(f"{args.name} is already onboarded")
        _show(rec)
        return 0
    action = _action(rec, args)
    if not action:
        print("the repositories can't change after confirm; delete "
              f"{state_dir}/onboarding/{args.name}.json and close its infra PR "
              "to start over", file=sys.stderr)
        return 1
    _write(state_dir, action, args.name, {} if action == "resume" else _request(args))
    rec = _kick(sh, state_dir, args.name)
    if _at_confirm(rec) and _yes(rec):
        _write(state_dir, "confirm", args.name, {"revision": rec["revision"]})
        _kick(sh, state_dir, args.name)
    return 0


def _action(rec: dict | None, args: argparse.Namespace) -> str:
    """The intent a run of `project add` writes; "" when it is refused."""
    if rec is None:
        return "start"
    if _same(rec["request"]["repos"], args.repos):
        return "resume"
    return "" if rec["confirmed_revision"] else "edit"


def _same(a: list[str], b: list[str]) -> bool:
    def norm(repos):
        return [(onboard.slug(x) or x).lower() for x in repos]
    return norm(a) == norm(b)


def _at_confirm(rec: dict) -> bool:
    return any(s["name"] == "confirm" and s["state"] == "blocked" for s in rec["steps"])


def _yes(rec: dict) -> bool:
    print(yaml.safe_dump(rec["proposal"], sort_keys=False))
    try:
        return input(f"confirm {rec['revision'][:12]}? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def _request(args: argparse.Namespace) -> dict:
    return {"repos": args.repos, "board": args.board,
            "commands": [_commands(repo) for repo in args.repos]}


def _commands(repo: str) -> dict:
    out = {"setup_cmd": _ask(f"{repo} setup_cmd (empty skips): ")}
    while not (gate := _ask(f"{repo} gate_cmd (required): ")):
        pass
    return {**out, "gate_cmd": gate, "verify_cmd": _ask(f"{repo} verify_cmd (empty skips): ")}


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise SystemExit("\nstdin ended before every command was given") from None


def _write(state_dir: str, action: str, project: str, payload: dict) -> None:
    onboard.write_intent(state_dir, action, project, payload,
                         actor=getpass.getuser(), epoch_ms=time.time_ns() // 1_000_000)


def _kick(sh: onboard.Sh, state_dir: str, project: str) -> dict:
    """Start one engine run, wait for it, print the record and return it."""
    sh(KICK)
    rec = onboard.load_record(state_dir, project)
    _show(rec)
    return rec


def _show(rec: dict) -> None:
    print(f"\n{rec['project']}: {rec['status']}")
    for s in rec["steps"]:
        print(f"  {s['name']:<13} {s['state']:<9} {s['detail']} {s['link']}".rstrip())
    if rec["notice"]:
        print(f"notice: {rec['notice']}")
