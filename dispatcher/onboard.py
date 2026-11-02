"""Project onboarding: the engine of the agent-ops-onboard unit.

`agent-ops project add` (dispatcher.onboard_cli) writes one intent file per
action into state_dir/onboarding-intents/ and starts the unit. Each run takes
onboarding.lock, applies then deletes the intents in lexical order, and works
the first unverified step of every open record in state_dir/onboarding/,
saving after each step and stopping a record at its first step that isn't
verified. Blocked and failed steps are retried on every run; a timer reruns
the unit for the long waits. Only this module writes the records.

Every subprocess goes through `sh(argv, cwd=None, env=None) -> str` (raises
CalledProcessError) and every HTTP request through `get(url, headers) ->
bytes`: the two seams tests fake."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from dispatcher import board, config
from dispatcher.convergence import pass_lock
from dispatcher.state import write_json_atomic

INTENTS_DIR = "onboarding-intents"
RECORDS_DIR = "onboarding"
ACTIONS = ("start",)

Sh = Callable[..., str]
Get = Callable[[str, dict], bytes]


def sh(argv: list[str], cwd: str | None = None, env: dict | None = None) -> str:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                          check=True, timeout=1800).stdout


def get(url: str, headers: dict) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                timeout=10) as resp:
        return resp.read()


class Blocked(Exception):
    """The operator must act: `detail` names the action, `link` points at it."""

    def __init__(self, detail: str, link: str = ""):
        super().__init__(detail)
        self.link = link


@dataclass(frozen=True)
class Run:
    config_path: Path
    cfg: config.Config
    sh: Sh
    get: Get
    now: datetime


# -- intents ------------------------------------------------------------------


def write_intent(state_dir: str | Path, action: str, project: str, payload: dict,
                 actor: str, epoch_ms: int) -> Path:
    if action not in ACTIONS:
        raise ValueError(f"unknown onboarding action: {action!r}")
    p = Path(state_dir) / INTENTS_DIR / f"{epoch_ms}-{project}-{action}.json"
    write_json_atomic(p, {"action": action, "project": project, "actor": actor,
                          "created_at": datetime.now(timezone.utc).isoformat(),
                          "payload": payload})
    return p


def _drain(r: Run) -> None:
    """Apply then delete every intent, in lexical order: at most once."""
    d = Path(r.cfg.state_dir) / INTENTS_DIR
    for p in sorted(d.glob("*.json")):
        try:
            _apply(r, json.loads(p.read_text()))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            print(f"[warn] malformed onboarding intent {p.name} deleted: {e!r}",
                  file=sys.stderr)
        p.unlink(missing_ok=True)


def _apply(r: Run, intent: dict) -> None:
    action, project = intent["action"], intent["project"]
    if action not in ACTIONS or not config._SLUG.fullmatch(project):
        raise ValueError(f"action {action!r} for project {project!r}")
    _APPLY[action](r, project, intent)


def _start(r: Run, project: str, intent: dict) -> None:
    if load_record(r.cfg.state_dir, project) is not None:
        print(f"[warn] onboarding {project}: already started; start ignored",
              file=sys.stderr)
        return
    _save(r, {"project": project, "status": "open",
              "started_by": str(intent.get("actor", "")),
              "request": _request(intent["payload"]), "proposal": {},
              "revision": "", "confirmed_revision": "", "notice": "",
              "steps": [{"name": n, "state": "pending", "detail": "", "link": "",
                         "since": ""} for n in STEPS],
              "infra_pr": {}, "resources": []})


def _request(payload: dict) -> dict:
    """The validated repos, board and per-repository commands of a start."""
    repos, b, commands = payload["repos"], payload["board"], payload["commands"]
    if not repos or len(commands) != len(repos):
        raise ValueError("repos must be non-empty, with one commands entry each")
    return {"repos": [str(x) for x in repos],
            "board": {"owner": str(b["owner"]), "number": int(b["number"])},
            "commands": [{k: str(c.get(k) or "") for k in
                          ("setup_cmd", "gate_cmd", "verify_cmd")} for c in commands]}


_APPLY = {"start": _start}


# -- records ------------------------------------------------------------------


def _record_path(state_dir: str | Path, project: str) -> Path:
    return Path(state_dir) / RECORDS_DIR / f"{project}.json"


def load_record(state_dir: str | Path, project: str) -> dict | None:
    p = _record_path(state_dir, project)
    return json.loads(p.read_text()) if p.exists() else None


def _records(state_dir: str | Path) -> list[dict]:
    return [json.loads(p.read_text())
            for p in sorted((Path(state_dir) / RECORDS_DIR).glob("*.json"))]


def _save(r: Run, rec: dict) -> None:
    rec["updated_at"] = r.now.isoformat()
    write_json_atomic(_record_path(r.cfg.state_dir, rec["project"]), rec)


def revision(proposal: dict) -> str:
    return hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()


# -- the run ------------------------------------------------------------------


def run(config_path: str | Path, sh: Sh = sh, get: Get = get,
        now: datetime | None = None) -> None:
    cfg = config.load_config(config_path)
    r = Run(Path(config_path), cfg, sh, get, now or datetime.now(timezone.utc))
    with pass_lock(cfg.state_dir, name="onboarding.lock"):
        _drain(r)
        for rec in _records(cfg.state_dir):
            if rec["status"] == "open":
                _advance(r, rec)


def _advance(r: Run, rec: dict) -> None:
    """Work the record's steps in order; stop at the first not verified."""
    for s in rec["steps"]:
        if s["state"] == "verified":
            continue
        state, detail, link = _work(r, rec, s["name"])
        if state != s["state"]:
            s["since"] = r.now.isoformat()
        s.update(state=state, detail=detail, link=link)
        _save(r, rec)
        if state != "verified":
            return


def _work(r: Run, rec: dict, name: str) -> tuple[str, str, str]:
    try:
        return _STEPS[name](r, rec)
    except Blocked as e:
        return "blocked", str(e), e.link
    except Exception as e:  # any other error fails the step; every run retries it
        return "failed", (getattr(e, "stderr", None) or repr(e)).strip()[-2000:], ""


# -- resolve ------------------------------------------------------------------

_GITHUB = re.compile(r"(?:https://github\.com/|(?:ssh://)?git@github\.com[:/])?"
                     r"([\w.-]+/[\w.-]+?)(?:\.git)?/?")


def slug(spelling: str) -> str | None:
    """"owner/name" of a GitHub repository spelt as a slug, an https or ssh
    URL, with or without .git; None when it is none of these."""
    m = _GITHUB.fullmatch(spelling.strip())
    return m.group(1) if m else None


def _resolve(r: Run, rec: dict) -> tuple[str, str, str]:
    found = [_github_repo(r, x) for x in rec["request"]["repos"]]
    holders = _holders(r, rec["project"])
    for x in found:
        holder = holders.get(x["github_id"]) or holders.get(x["repo"].lower())
        if holder:
            raise Blocked(f"{x['repo']} already belongs to project {holder!r}")
    proposal = _proposal(rec, found)
    _check_board(r, proposal["board"])
    _check_fits(r, proposal)
    rec["proposal"], rec["revision"] = proposal, revision(proposal)
    return "verified", "", ""


def _github_repo(r: Run, spelling: str) -> dict:
    s = slug(spelling)
    if not s:
        raise Blocked(f"{spelling!r} is not a GitHub repository (owner/name or its URL)")
    try:
        d = json.loads(r.sh(["gh", "api", f"repos/{s}"]))
    except subprocess.CalledProcessError as e:
        if "404" not in (e.stderr or ""):
            raise
        raise Blocked(f"{s}: no such repository on GitHub, or the box's "
                      "token can't read it") from None
    return {"name": d["name"], "repo": d["full_name"], "github_id": d["id"]}


def _holders(r: Run, project: str) -> dict:
    """github_id and lowercased owner/name -> the project holding it: a
    config project or another open record (R1)."""
    held = {}
    for x in r.cfg.repositories:
        held[x.github_id] = held[x.repo.lower()] = x.project.name
    for other in _records(r.cfg.state_dir):
        if other["status"] == "open" and other["project"] != project:
            for x in other["proposal"].get("repositories", []):
                held[x["github_id"]] = held[x["repo"].lower()] = other["project"]
    return held


def _proposal(rec: dict, found: list[dict]) -> dict:
    """The targets.yaml block: paused, paths derived (#226)."""
    root = Path.home() / "repos" / rec["project"]
    return {"name": rec["project"], "paused": True, "board": rec["request"]["board"],
            "repositories": [{**x, "clone_path": str(root / x["name"]),
                              "worktrees_path": str(root / f"{x['name']}.worktrees"),
                              **commands}
                             for x, commands in zip(found, rec["request"]["commands"])]}


def _check_board(r: Run, b: dict) -> None:
    try:
        board.fields(b["owner"], b["number"],
                     lambda args: r.sh(["gh", *args], env=board.project_env()))
    except board.BoardError as e:
        raise Blocked(str(e)) from None


def _check_fits(r: Run, proposal: dict) -> None:
    raw = yaml.safe_load(r.config_path.read_text())
    raw["projects"] = [*(raw.get("projects") or []), proposal]
    try:
        config.parse_config(raw)
    except ValueError as e:
        raise Blocked(f"the live config refuses the proposal: {e}") from None


# -- clone --------------------------------------------------------------------


def _clone(r: Run, rec: dict) -> tuple[str, str, str]:
    for x in rec["proposal"]["repositories"]:
        _clone_one(r, Path(x["clone_path"]), x["repo"])
        _resource(rec, "clone", x["clone_path"])
    return "verified", "", ""


def _clone_one(r: Run, path: Path, repo: str) -> None:
    """Clone into <path>.partial, then rename: a path that exists is a whole
    clone. The repository at the path must be `repo`."""
    if not path.exists():
        partial = path.with_name(f"{path.name}.partial")
        shutil.rmtree(partial, ignore_errors=True)
        partial.parent.mkdir(parents=True, exist_ok=True)
        r.sh(["gh", "repo", "clone", repo, str(partial)])
        partial.rename(path)
    origin = slug(r.sh(["git", "-C", str(path), "remote", "get-url", "origin"]))
    if (origin or "").lower() != repo.lower():
        raise Blocked(f"{path} holds {origin}, not {repo}: move it away, then resume")


def _resource(rec: dict, kind: str, ref: str) -> None:
    entry = {"kind": kind, "ref": ref, "state": "in use"}
    if entry not in rec["resources"]:
        rec["resources"].append(entry)


STEPS = ("resolve", "clone")
_STEPS = {"resolve": _resolve, "clone": _clone}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m dispatcher.onboard")
    parser.add_argument("--config", default=os.environ.get(
        "AGENT_OPS_CONFIG", str(Path.home() / "agent-ops-state" / "targets.yaml")))
    run(parser.parse_args(argv).config)


if __name__ == "__main__":
    main()
