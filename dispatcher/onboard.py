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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

import yaml

from dispatcher import board, config, intents
from dispatcher.infra_config import add_project_text
from dispatcher.state import state_lock, write_json_atomic

RECORDS_DIR = "onboarding"
INFRA_REPO = "jesdi/agent-ops-infra"
INFRA_TARGETS = "config/targets.yaml"
# git authenticates as gh does, so GH_TOKEN in the env picks the token.
GIT_AS_GH = ["git", "-c", "credential.helper=", "-c",
             "credential.helper=!gh auth git-credential"]

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

QUEUE = intents.Queue("onboarding-intents", ("start", "edit", "confirm", "resume"),
                      ("project",))


class Intent(NamedTuple):
    action: str
    project: str
    payload: dict    # validated: a request, a revision or nothing
    actor: str
    path: Path


def write_intent(state_dir: str | Path, action: str, project: str, payload: dict,
                 actor: str, epoch_ms: int) -> Path:
    return QUEUE.write(state_dir, action, (project,), payload, actor, epoch_ms)


def _intent(rec: dict, path: Path) -> Intent:
    action, project, payload = rec["action"], rec["project"], rec["payload"]
    if action not in QUEUE.actions or not config._SLUG.fullmatch(project):
        raise ValueError(f"action {action!r} for project {project!r}")
    if action in ("start", "edit"):
        payload = _request(payload)
    elif action == "confirm":
        payload = {"revision": str(payload["revision"])}
    return Intent(action, project, payload, str(rec.get("actor", "")), path)


def _drain(r: Run) -> None:
    """Apply then delete every intent, in lexical order: at most once."""
    for it in QUEUE.read(r.cfg.state_dir, _intent):
        _APPLY[it.action](r, it)
        it.path.unlink(missing_ok=True)


def _start(r: Run, it: Intent) -> None:
    project = it.project
    if load_record(r.cfg.state_dir, project) is not None:
        print(f"[warn] onboarding {project}: already started; start ignored",
              file=sys.stderr)
        return
    _save(r, {"project": project, "status": "open",
              "started_by": it.actor, "request": it.payload, "proposal": {},
              "revision": "", "confirmed_revision": "", "notice": "",
              "steps": _pending(), "infra_pr": {}, "resources": []})


def _pending() -> list[dict]:
    return [{"name": n, "state": "pending", "detail": "", "link": "", "since": ""}
            for n in STEPS]


def _open_record(r: Run, project: str, action: str) -> dict | None:
    rec = load_record(r.cfg.state_dir, project)
    if rec is None or rec["status"] != "open":
        print(f"[warn] {action} for {project}: no open onboarding; dropped",
              file=sys.stderr)
        return None
    return rec


def _edit(r: Run, it: Intent) -> None:
    """Before confirm: a new request, resolved again from the first step."""
    if not (rec := _open_record(r, it.project, "edit")):
        return
    if rec["confirmed_revision"]:
        rec["notice"] = "edit refused: the repositories can't change after confirm"
    else:
        rec.update(request=it.payload, proposal={}, revision="",
                   notice="", steps=_pending())
    _save(r, rec)


def _confirm(r: Run, it: Intent) -> None:
    """Confirm names the revision it saw; a stale one is refused, as the
    console refuses stale model settings."""
    if not (rec := _open_record(r, it.project, "confirm")):
        return
    seen = it.payload["revision"]
    if seen == rec["revision"]:
        rec.update(confirmed_revision=seen, notice="")
    else:
        rec["notice"] = (f"confirm of revision {seen[:12]} refused: the proposal is "
                         f"now revision {rec['revision'][:12]}; review it and confirm again")
    _save(r, rec)


def _resume(r: Run, it: Intent) -> None:
    """Every run retries blocked and failed steps: a resume only kicks one."""
    _open_record(r, it.project, "resume")


def _request(payload: dict) -> dict:
    """The validated repos, board and per-repository commands of a start."""
    repos, b, commands = payload["repos"], payload["board"], payload["commands"]
    if not repos or len(commands) != len(repos):
        raise ValueError("repos must be non-empty, with one commands entry each")
    return {"repos": [str(x) for x in repos],
            "board": {"owner": str(b["owner"]), "number": int(b["number"])},
            "commands": [{k: str(dict(c).get(k) or "") for k in
                          ("setup_cmd", "gate_cmd", "verify_cmd")} for c in commands]}


_APPLY = {"start": _start, "edit": _edit, "confirm": _confirm, "resume": _resume}


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
    with state_lock(cfg.state_dir, "onboarding.lock"):
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
    rec["status"] = "complete"
    _save(r, rec)


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
    root = Path(config.REPOS_ROOT).expanduser() / rec["project"]
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


# -- confirm ------------------------------------------------------------------


def _confirm_step(r: Run, rec: dict) -> tuple[str, str, str]:
    if rec["revision"] and rec["confirmed_revision"] == rec["revision"]:
        return "verified", "", ""
    req = rec["request"]
    raise Blocked(f"review the proposal, then confirm revision {rec['revision'][:12]}: "
                  f"agent-ops project add {rec['project']} {' '.join(req['repos'])} "
                  f"--board {req['board']['owner']}/{req['board']['number']}")


# -- config-pr ----------------------------------------------------------------


def _infra_env() -> dict:
    """The env of every agent-ops-infra call: GH_TOKEN is the infra token,
    which only the onboarding unit loads."""
    token = os.environ.get("GH_INFRA_TOKEN")
    if not token:
        raise Blocked("GH_INFRA_TOKEN is unset: agent-ops-onboard loads it from "
                      "its op.env entry; run project add through that unit")
    return {**os.environ, "GH_TOKEN": token}


def _scratch(r: Run) -> Path:
    return Path(r.cfg.state_dir) / RECORDS_DIR / "infra"


def _git(r: Run, env: dict, *args: str) -> str:
    return r.sh([*GIT_AS_GH, "-C", str(_scratch(r)), *args], env=env)


def _infra_main(r: Run, env: dict) -> str:
    """infra main's targets.yaml, fetched into the scratch clone."""
    if not (_scratch(r) / ".git").exists():
        shutil.rmtree(_scratch(r), ignore_errors=True)
        r.sh([*GIT_AS_GH, "clone", f"https://github.com/{INFRA_REPO}.git",
              str(_scratch(r))], env=env)
    _git(r, env, "fetch", "origin")
    return _git(r, env, "show", f"origin/main:{INFRA_TARGETS}")


def _on_main(text: str, project: str) -> bool:
    return any(p.get("name") == project
               for p in yaml.safe_load(text).get("projects") or [])


def _open_pr(r: Run, project: str, env: dict) -> dict | None:
    rows = json.loads(r.sh(["gh", "pr", "list", "--repo", INFRA_REPO, "--head",
                            f"onboard/{project}", "--state", "open",
                            "--json", "number,url,headRefOid"], env=env))
    return rows[0] if rows else None


def _config_pr(r: Run, rec: dict) -> tuple[str, str, str]:
    env = _infra_env()
    main = _infra_main(r, env)
    if _on_main(main, rec["project"]):
        return "verified", "already on infra main", rec["infra_pr"].get("url", "")
    pr, ip = _open_pr(r, rec["project"], env), rec["infra_pr"]
    if not (pr and pr["headRefOid"] == ip.get("head")
            and ip.get("revision") == rec["revision"]):
        _build_pr(r, rec, main, pr, env)
    return "verified", "", rec["infra_pr"]["url"]


def _build_pr(r: Run, rec: dict, main: str, pr: dict | None, env: dict) -> None:
    """onboard/<project> rebuilt on infra main: the project added, checked
    with the loader, committed and force-pushed. The PR is opened only when
    none is open on the branch."""
    project, branch = rec["project"], f"onboard/{rec['project']}"
    text = add_project_text(main, yaml.safe_dump([rec["proposal"]], sort_keys=False))
    try:
        config.parse_config(yaml.safe_load(text))
    except ValueError as e:
        raise Blocked(f"infra's targets.yaml refuses the project: {e}") from None
    _git(r, env, "checkout", "-B", branch, "origin/main")
    (_scratch(r) / INFRA_TARGETS).write_text(text)
    _git(r, env, "add", INFRA_TARGETS)
    _git(r, env, "-c", "user.name=agent-ops-onboard", "-c",
         "user.email=agent-ops-onboard@users.noreply.github.com",
         "commit", "-m", f"feat(config): onboard {project}")
    head, base = (_git(r, env, "rev-parse", ref).strip() for ref in ("HEAD", "origin/main"))
    _git(r, env, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    pr = pr or _create_pr(r, rec, env)
    rec["infra_pr"] = {"number": pr["number"], "url": pr["url"], "head": head, "base": base,
                       "revision": rec["revision"], "pushed_at": r.now.isoformat(),
                       "merge_sha": "", "merged_at": ""}
    _resource(rec, "infra-pr", pr["url"])


def _create_pr(r: Run, rec: dict, env: dict) -> dict:
    url = r.sh(["gh", "pr", "create", "--repo", INFRA_REPO, "--base", "main",
                "--head", f"onboard/{rec['project']}",
                "--title", f"feat(config): onboard {rec['project']}",
                "--body", f"Adds project {rec['project']}, paused, at proposal "
                          f"revision {rec['revision'][:12]}. Opened by agent-ops-onboard, "
                          "which squash-merges it once its checks are green."],
               env=env).strip()
    return {"number": int(url.rsplit("/", 1)[1]), "url": url}


# -- config-merge -------------------------------------------------------------

CI_LIMIT = timedelta(minutes=30)
GREEN = frozenset({"success", "skipped", "neutral"})


def _config_merge(r: Run, rec: dict) -> tuple[str, str, str]:
    """Squash-merge the infra PR once its ci run and every check on its head
    are green (#225): GitHub enforces nothing on agent-ops-infra."""
    env, ip = _infra_env(), rec["infra_pr"]
    pr = json.loads(r.sh(["gh", "pr", "view", str(ip["number"]), "--repo", INFRA_REPO,
                          "--json", "state,mergeCommit,mergedAt"], env=env))
    if pr["state"] == "MERGED":
        ip.update(merge_sha=pr["mergeCommit"]["oid"],
                  merged_at=datetime.fromisoformat(pr["mergedAt"]).isoformat())
        return "verified", "", ip["url"]
    if pr["state"] != "OPEN":
        raise Blocked("the infra PR was closed: reopen it, or delete the record "
                      "to start over", ip["url"])
    main = _infra_main(r, env)
    if _git(r, env, "rev-parse", "origin/main").strip() != ip["base"]:
        _build_pr(r, rec, main, {"number": ip["number"], "url": ip["url"]}, env)
        return "waiting", "infra main moved: the PR was rebuilt on it", ip["url"]
    return _merge_when_green(r, ip, env)


def _merge_when_green(r: Run, ip: dict, env: dict) -> tuple[str, str, str]:
    waiting_on = _ci_waiting_on(r, ip["head"], env)
    if not waiting_on:
        return _squash(r, ip, env)
    if r.now - datetime.fromisoformat(ip["pushed_at"]) > CI_LIMIT:
        raise Blocked(f"{waiting_on}, 30 minutes after the push", ip["url"])
    return "waiting", waiting_on, ip["url"]


def _ci_waiting_on(r: Run, head: str, env: dict) -> str:
    """What the checks on `head` still wait for; "" once its ci run exists
    and every check is green. A red check blocks, with its link."""
    runs = json.loads(r.sh(["gh", "api", f"repos/{INFRA_REPO}/actions/runs?head_sha="
                            f"{head}&per_page=100"], env=env))["workflow_runs"]
    checks = json.loads(r.sh(["gh", "api", f"repos/{INFRA_REPO}/commits/{head}/"
                              "check-runs?per_page=100"], env=env))["check_runs"]
    for c in checks:
        if c["status"] == "completed" and c["conclusion"] not in GREEN:
            raise Blocked(f"infra check {c['name']} is {c['conclusion']}", c["html_url"])
    pending = [c["name"] for c in checks if c["status"] != "completed"]
    if pending:
        return f"checks pending: {', '.join(pending)}"
    return "" if any(x["name"] == "ci" for x in runs) else "no ci run on the head yet"


def _squash(r: Run, ip: dict, env: dict) -> tuple[str, str, str]:
    try:
        out = r.sh(["gh", "api", "-X", "PUT", f"repos/{INFRA_REPO}/pulls/{ip['number']}/merge",
                    "-f", "merge_method=squash", "-f", f"sha={ip['head']}"], env=env)
    except subprocess.CalledProcessError as e:
        raise Blocked(f"GitHub refused the merge: {(e.stderr or '').strip()}",
                      ip["url"]) from None
    ip.update(merge_sha=json.loads(out)["sha"], merged_at=r.now.isoformat())
    return "verified", "", ip["url"]


# -- deploy -------------------------------------------------------------------

DEPLOY_LIMIT = timedelta(minutes=15)
UPDATER = "agent-ops-update.service"


def _deploy(r: Run, rec: dict) -> tuple[str, str, str]:
    """agent-ops-update deploys the merge (#225): verified once the box's
    infra checkout holds it and the live config loads with the project
    paused."""
    ip = rec["infra_pr"]
    if _box_has(r, ip["merge_sha"]) and _live_paused(r, rec["project"]):
        return "verified", "", ""
    merged_at = datetime.fromisoformat(ip["merged_at"])
    if journal := _updater_failure(r, merged_at):
        raise Blocked(f"{UPDATER} failed after the merge:\n{journal}")
    if r.now - merged_at > DEPLOY_LIMIT:
        raise Blocked("nothing deployed 15 minutes after the merge: is "
                      "agent-ops-update.timer running? "
                      "(systemctl --user status agent-ops-update.timer)")
    return "waiting", f"waiting for {UPDATER} to deploy the merge", ""


def _box_has(r: Run, sha: str) -> bool:
    try:
        r.sh(["git", "-C", os.path.expanduser(config.INFRA_CHECKOUT), "merge-base",
              "--is-ancestor", sha, "HEAD"])
    except subprocess.CalledProcessError:
        return False
    return True


def _live_paused(r: Run, project: str) -> bool:
    """Read now, not at the run's start: the updater may have just
    installed it. One the loader refuses fails the step."""
    return any(p.name == project and p.paused
               for p in config.load_config(r.config_path).projects)


def _updater_failure(r: Run, since: datetime) -> str:
    """The updater's last journal lines when its last run failed after
    `since`; "" otherwise."""
    out = r.sh(["systemctl", "--user", "show", UPDATER, "-p", "Result",
                "-p", "ExecMainExitTimestamp", "--timestamp=unix"])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    ended = props.get("ExecMainExitTimestamp", "").lstrip("@")
    if props.get("Result") == "success" or not ended.isdigit() or (
            datetime.fromtimestamp(int(ended), timezone.utc) <= since):
        return ""
    return r.sh(["journalctl", "--user", "-u", UPDATER, "-n", "15",
                 "--no-pager", "-o", "cat"]).strip()


# -- visible ------------------------------------------------------------------

VISIBLE_LIMIT = timedelta(minutes=15)
# The web console reads targets.yaml only at startup, so ask the running one.
LOGIN = "agent-ops-onboard"


def _visible(r: Run, rec: dict) -> tuple[str, str, str]:
    if _listed_paused(r, rec["project"]):
        return "verified", "", ""
    deployed = next(s["since"] for s in rec["steps"] if s["name"] == "deploy")
    if r.now - datetime.fromisoformat(deployed) > VISIBLE_LIMIT:
        raise Blocked("the console doesn't list the project 15 minutes after the "
                      "deploy: check agent-ops-web.service "
                      "(systemctl --user status agent-ops-web.service)")
    return "waiting", "waiting for the console to list the project", ""


def _listed_paused(r: Run, project: str) -> bool:
    try:
        body = r.get(f"http://127.0.0.1:{config.web_port()}/api/projects",
                     {config.LOGIN_HEADER: LOGIN})
    except OSError:  # refused while the updater restarts it
        return False
    return any(p["name"] == project and p["paused"]
               for p in json.loads(body)["projects"])


STEPS = ("resolve", "clone", "confirm", "config-pr", "config-merge", "deploy", "visible")
_STEPS = {"resolve": _resolve, "clone": _clone, "confirm": _confirm_step,
          "config-pr": _config_pr, "config-merge": _config_merge, "deploy": _deploy,
          "visible": _visible}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m dispatcher.onboard")
    parser.add_argument("--config", default=config.default_config_path())
    run(parser.parse_args(argv).config)


if __name__ == "__main__":
    main()
