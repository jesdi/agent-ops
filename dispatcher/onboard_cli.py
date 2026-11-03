"""Terminal onboarding: inspect first, then review or fill commands by hand.

Run on the box as `agent`. The unit owns progress; this client writes durable
intents and displays its record. `--manual` prompts immediately and skips a
pending draft. A later invocation offers detected commands as editable defaults.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time

from dispatcher import config, onboard
from dispatcher.infra_config import project_block

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
    add.add_argument("--board", type=_board, help="owner/number")
    add.add_argument("--manual", action="store_true", help="fill commands by hand without waiting for a draft")
    add.add_argument("--config", default=config.default_config_path())
    return parser


def main(argv: list[str] | None = None, sh: onboard.Sh = onboard.sh) -> int:
    args = _parser().parse_args(argv)
    if getpass.getuser() != USER:
        print(f"run agent-ops as {USER} (sudo -iu {USER})", file=sys.stderr)
        return 2
    state_dir = config.load_config(args.config).state_dir
    existing = onboard.load_record(state_dir, args.name)
    if existing and existing.status == "complete":
        _show(existing, "already onboarded")  # informational: no prompts, no writes
        return 0
    if args.manual and existing and existing.request.get("inspect"):
        _write(state_dir, "skip-inspection", args.name, {"revision": existing.revision})
    _write(state_dir, "add", args.name, _request(args, existing))
    rec = _kick(sh, state_dir, args.name)
    if rec.awaiting == "confirm" and onboard.can_confirm(rec) and _yes(rec):
        _write(state_dir, "confirm", args.name, {"revision": rec.revision})
        _kick(sh, state_dir, args.name)
    return 0


def _yes(rec: onboard.Record) -> bool:
    print(project_block(rec.proposal))
    try:
        return input(f"confirm {rec.revision[:12]}? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def _request(args: argparse.Namespace, rec: onboard.Record | None) -> dict:
    """The request of this run. A repository already in the record offers
    its commands as the answers Enter keeps."""
    if not args.manual and (rec is None or rec.awaiting != "confirm"):
        return _inspection_request(args, rec)
    current = _current_commands(rec)
    board_value = args.board or (rec.proposal.get("board") if rec else None)
    if not board_value:
        board_value = _board(_ask("GitHub board", "", "owner/number"))
    result = {"repos": args.repos, "board": board_value,
              "commands": [_commands(repo, current.get(onboard.repo_key(repo), {}))
                           for repo in args.repos]}
    if rec and rec.request.get("inspect"):
        result["inspect"] = True
    return result


def _inspection_request(args, rec):
    if rec and list(map(onboard.repo_key, args.repos)) == list(map(onboard.repo_key, rec.request["repos"])):
        return {**rec.request, "board": args.board or rec.request["board"]}
    return {"repos": args.repos, "board": args.board,
            "commands": [{} for _ in args.repos], "inspect": True}


def _current_commands(rec):
    if rec is None:
        return {}
    return {onboard.repo_key(item["repo"]): item
            for item in rec.proposal.get("repositories", [])}


def _commands(repo: str, current: dict) -> dict:
    def ask(key: str, hint: str) -> str:
        return _ask(f"{repo} {key}", current.get(key, ""), hint)
    out = {"setup_cmd": ask("setup_cmd", "empty skips")}
    while not (gate := ask("gate_cmd", "required")):
        pass
    result = {**out, "gate_cmd": gate, "verify_cmd": ask("verify_cmd", "empty skips")}
    if "services" in current:
        result["services"] = current["services"]
    return result


def _ask(name: str, default: str, hint: str) -> str:
    try:
        answer = input(f"{name} [{default}]: " if default else f"{name} ({hint}): ")
    except EOFError:
        raise SystemExit("\nstdin ended before every command was given") from None
    return answer.strip() or default


def _write(state_dir: str, action: str, project: str, payload: dict) -> None:
    onboard.write_intent(state_dir, action, project, payload,
                         actor=getpass.getuser(), epoch_ms=time.time_ns() // 1_000_000)


def _kick(sh: onboard.Sh, state_dir: str, project: str) -> onboard.Record:
    """Start one engine run, wait for it, print the record and return it."""
    sh(KICK)
    rec = onboard.load_record(state_dir, project)
    _show(rec)
    return rec


def _show(rec: onboard.Record, notice: str | None = None) -> None:
    print(f"\n{rec.project}: {rec.status}")
    for s in rec.steps:
        print(f"  {s.name:<13} {s.state:<9} {s.detail} {s.link}".rstrip())
    if notice := rec.notice if notice is None else notice:
        print(f"notice: {notice}")
