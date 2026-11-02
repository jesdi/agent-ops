"""Project onboarding: the engine of the agent-ops-onboard unit.

`agent-ops project add` (dispatcher.onboard_cli) writes one intent file per
action into state_dir/onboarding-intents/ and starts the unit. Each run takes
onboarding.lock, applies then deletes the intents in lexical order, and works
the first unverified step of every open record in state_dir/onboarding/,
saving after each step and stopping a record at its first step that isn't
verified. Blocked and failed steps are retried on every run; a timer reruns
the unit for the long waits. Only this module writes the records, and their
layout is private to it.

Every step returns an Outcome; an exception it doesn't expect fails it.
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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, NamedTuple

import yaml

from dispatcher import board, config, intents
from dispatcher.infra_config import add_project_text
from dispatcher.pr_poll import ci_verdict
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


@dataclass(frozen=True)
class Run:
    config_path: Path
    cfg: config.Config
    sh: Sh
    get: Get
    now: datetime


# -- the record ---------------------------------------------------------------


class StepState(StrEnum):
    PENDING = "pending"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    VERIFIED = "verified"


@dataclass(frozen=True)
class Outcome:
    """What a step found. Blocked: the operator must act, `detail` names the
    action and `link` points at it."""
    state: StepState
    detail: str = ""
    link: str = ""


VERIFIED = Outcome(StepState.VERIFIED)


def blocked(detail: str, link: str = "") -> Outcome:
    return Outcome(StepState.BLOCKED, detail, link)


def waiting(detail: str, link: str = "") -> Outcome:
    return Outcome(StepState.WAITING, detail, link)


@dataclass
class Step:
    name: str
    state: StepState = StepState.PENDING
    detail: str = ""
    link: str = ""
    since: str = ""


@dataclass
class InfraPr:
    number: int
    url: str
    head: str
    base: str
    revision: str
    pushed_at: str


@dataclass
class Record:
    project: str
    status: str = "open"                 # open | complete
    started_by: str = ""
    updated_at: str = ""
    request: dict = field(default_factory=dict)
    proposal: dict = field(default_factory=dict)    # the targets.yaml block
    revision: str = ""
    confirmed_revision: str = ""
    notice: str = ""
    steps: list[Step] = field(default_factory=lambda: [Step(n) for n in STEPS])
    infra_pr: InfraPr | None = None
    merge_sha: str = ""                  # the infra main commit that holds the project
    merged_at: str = ""
    resources: list[dict] = field(default_factory=list)
    deployed_at: str = ""
    awaiting: str = ""                   # "confirm" while the operator must confirm

    @property
    def pr_url(self) -> str:
        return self.infra_pr.url if self.infra_pr else ""

    def step(self, name: str) -> Step:
        return next(s for s in self.steps if s.name == name)

    @classmethod
    def from_json(cls, d: dict) -> Record:
        ip = d.get("infra_pr")
        return cls(**{**d, "steps": [Step(**{**s, "state": StepState(s["state"])})
                                     for s in d["steps"]],
                      "infra_pr": InfraPr(**ip) if ip else None})


def _record_path(state_dir: str | Path, project: str) -> Path:
    return Path(state_dir) / RECORDS_DIR / f"{project}.json"


def load_record(state_dir: str | Path, project: str) -> Record | None:
    p = _record_path(state_dir, project)
    return Record.from_json(json.loads(p.read_text())) if p.exists() else None


def _records(state_dir: str | Path) -> list[Record]:
    return [Record.from_json(json.loads(p.read_text()))
            for p in sorted((Path(state_dir) / RECORDS_DIR).glob("*.json"))]


def _save(r: Run, rec: Record) -> None:
    rec.updated_at = r.now.isoformat()
    rec.awaiting = "confirm" if rec.step("confirm").state is StepState.BLOCKED else ""
    write_json_atomic(_record_path(r.cfg.state_dir, rec.project), asdict(rec))


def revision(proposal: dict) -> str:
    return hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()


# -- intents ------------------------------------------------------------------

QUEUE = intents.Queue("onboarding-intents", ("add", "confirm"), ("project",))


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
    payload = (_request(payload) if action == "add"
               else {"revision": str(payload["revision"])})
    return Intent(action, project, payload, str(rec.get("actor", "")), path)


def _request(payload: dict) -> dict:
    """The validated repos, board and per-repository commands of an add."""
    repos, b, commands = payload["repos"], payload["board"], payload["commands"]
    if not repos or len(commands) != len(repos):
        raise ValueError("repos must be non-empty, with one commands entry each")
    return {"repos": [str(x) for x in repos],
            "board": {"owner": str(b["owner"]), "number": int(b["number"])},
            "commands": [{k: str(dict(c).get(k) or "") for k in
                          ("setup_cmd", "gate_cmd", "verify_cmd")} for c in commands]}


def _drain(r: Run) -> None:
    """Apply then delete every intent, in lexical order: at most once."""
    for it in QUEUE.read(r.cfg.state_dir, _intent):
        _APPLY[it.action](r, it)
        it.path.unlink(missing_ok=True)


def _add(r: Run, it: Intent) -> None:
    """A request opens a record, or reruns one under the rules of #210."""
    rec = load_record(r.cfg.state_dir, it.project)
    if rec is None:
        rec = Record(it.project, started_by=it.actor, request=it.payload)
    else:
        rec.notice = _rerun(rec, it.payload)
    _save(r, rec)


def _rerun(rec: Record, request: dict) -> str:
    """Apply a request to an existing record; the notice it leaves. The same
    request resumes. Before the merge, another one is resolved again from
    the first step and so goes back to confirm, but after confirm its
    repositories can't change."""
    if _same(rec.request, request):
        return "already onboarded" if rec.status == "complete" else ""
    if rec.step("config-merge").state is StepState.VERIFIED:
        return ("request refused: the project is merged into infra; edit "
                "targets.yaml by hand for now")
    if rec.confirmed_revision and _repo_keys(rec.request) != _repo_keys(request):
        return "request refused: the repositories can't change after confirm"
    rec.request, rec.steps = request, [Step(n) for n in STEPS]
    return ""


def repo_key(spelling: str) -> str:
    """One key for every spelling of a repository."""
    return (slug(spelling) or spelling).lower()


def _repo_keys(request: dict) -> list[str]:
    return [repo_key(x) for x in request["repos"]]


def _same(a: dict, b: dict) -> bool:
    return _repo_keys(a) == _repo_keys(b) and (a["board"], a["commands"]) == (
        b["board"], b["commands"])


def _confirm(r: Run, it: Intent) -> None:
    """Confirm names the revision it saw; a stale one is refused, as the
    console refuses stale model settings."""
    rec = load_record(r.cfg.state_dir, it.project)
    if rec is None or rec.status != "open":
        print(f"[warn] confirm for {it.project}: no open onboarding; dropped",
              file=sys.stderr)
        return
    seen = it.payload["revision"]
    if seen == rec.revision:
        rec.confirmed_revision, rec.notice = seen, ""
    else:
        rec.notice = (f"confirm of revision {seen[:12]} refused: the proposal is "
                      f"now revision {rec.revision[:12]}; review it and confirm again")
    _save(r, rec)


_APPLY = {"add": _add, "confirm": _confirm}


# -- the run ------------------------------------------------------------------


def run(config_path: str | Path, sh: Sh = sh, get: Get = get,
        now: datetime | None = None) -> None:
    cfg = config.load_config(config_path)
    r = Run(Path(config_path), cfg, sh, get, now or datetime.now(timezone.utc))
    with state_lock(cfg.state_dir, "onboarding.lock"):
        _drain(r)
        for rec in _records(cfg.state_dir):
            if rec.status == "open":
                _advance(r, rec)


def _advance(r: Run, rec: Record) -> None:
    """Work the record's steps in order; stop at the first not verified."""
    for s in rec.steps:
        if s.state is StepState.VERIFIED:
            continue
        out = _work(r, rec, s.name)
        if out.state != s.state:
            s.since = r.now.isoformat()
        s.state, s.detail, s.link = out.state, out.detail, out.link
        _save(r, rec)
        if out.state is not StepState.VERIFIED:
            return
    rec.status = "complete"
    _save(r, rec)


def _work(r: Run, rec: Record, name: str) -> Outcome:
    try:
        return _STEPS[name](r, rec)
    except Exception as e:  # any other error fails the step; every run retries it
        return Outcome(StepState.FAILED,
                       (getattr(e, "stderr", None) or repr(e)).strip()[-2000:])


# -- resolve ------------------------------------------------------------------

_GITHUB = re.compile(r"(?:https://github\.com/|(?:ssh://)?git@github\.com[:/])?"
                     r"([\w.-]+/[\w.-]+?)(?:\.git)?/?")


def slug(spelling: str) -> str | None:
    """"owner/name" of a GitHub repository spelt as a slug, an https or ssh
    URL, with or without .git; None when it is none of these."""
    m = _GITHUB.fullmatch(spelling.strip())
    return m.group(1) if m else None


def _resolve(r: Run, rec: Record) -> Outcome:
    found = []
    for spelling in rec.request["repos"]:
        x = _github_repo(r, spelling)
        if isinstance(x, Outcome):
            return x
        found.append(x)
    proposal = _proposal(rec, found)
    if out := (_held(r, rec.project, found) or _check_board(r, proposal["board"])
               or _check_fits(r, proposal)):
        return out
    rec.proposal, rec.revision = proposal, revision(proposal)
    return VERIFIED


def _github_repo(r: Run, spelling: str) -> dict | Outcome:
    s = slug(spelling)
    if not s:
        return blocked(f"{spelling!r} is not a GitHub repository (owner/name or its URL)")
    try:
        d = json.loads(r.sh(["gh", "api", f"repos/{s}"]))
    except subprocess.CalledProcessError as e:
        if "404" not in (e.stderr or ""):
            raise
        return blocked(f"{s}: no such repository on GitHub, or the box's "
                       "token can't read it")
    return {"name": d["name"], "repo": d["full_name"], "github_id": d["id"]}


def _held(r: Run, project: str, found: list[dict]) -> Outcome | None:
    holders = _holders(r, project)
    for x in found:
        if holder := holders.get(x["github_id"]) or holders.get(x["repo"].lower()):
            return blocked(f"{x['repo']} already belongs to project {holder!r}")
    return None


def _holders(r: Run, project: str) -> dict:
    """github_id and lowercased owner/name -> the project holding it: a
    config project or another open record (R1)."""
    held = {}
    for x in r.cfg.repositories:
        held[x.github_id] = held[x.repo.lower()] = x.project.name
    for other in _records(r.cfg.state_dir):
        if other.status == "open" and other.project != project:
            for x in other.proposal.get("repositories", []):
                held[x["github_id"]] = held[x["repo"].lower()] = other.project
    return held


def _proposal(rec: Record, found: list[dict]) -> dict:
    """The targets.yaml block: paused, paths derived (#226)."""
    root = Path(config.REPOS_ROOT).expanduser() / rec.project
    return {"name": rec.project, "paused": True, "board": rec.request["board"],
            "repositories": [{**x, "clone_path": str(root / x["name"]),
                              "worktrees_path": str(root / f"{x['name']}.worktrees"),
                              **commands}
                             for x, commands in zip(found, rec.request["commands"])]}


def _check_board(r: Run, b: dict) -> Outcome | None:
    try:
        board.fields(b["owner"], b["number"],
                     lambda args: r.sh(["gh", *args], env=board.project_env()))
    except board.BoardError as e:
        return blocked(str(e))
    return None


def _check_fits(r: Run, proposal: dict) -> Outcome | None:
    raw = yaml.safe_load(r.config_path.read_text())
    raw["projects"] = [*(raw.get("projects") or []), proposal]
    try:
        config.parse_config(raw)
    except ValueError as e:
        return blocked(f"the live config refuses the proposal: {e}")
    return None


# -- clone --------------------------------------------------------------------


def _clone(r: Run, rec: Record) -> Outcome:
    for x in rec.proposal["repositories"]:
        if out := _clone_one(r, Path(x["clone_path"]), x["repo"]):
            return out
        _resource(rec, "clone", x["clone_path"])
    return VERIFIED


def _clone_one(r: Run, path: Path, repo: str) -> Outcome | None:
    """Clone into <path>.partial, then rename: a path that exists is a whole
    clone. The repository at the path must be `repo`."""
    if not path.exists():
        partial = path.with_name(f"{path.name}.partial")
        if out := _clear_partial(r, partial, repo):
            return out
        partial.parent.mkdir(parents=True, exist_ok=True)
        r.sh(["gh", "repo", "clone", repo, str(partial)])
        partial.rename(path)
    origin = _origin(r, path)
    if origin != repo.lower():
        return blocked(f"{path} holds {origin}, not {repo}: move it away, then resume")
    return None


def _clear_partial(r: Run, partial: Path, repo: str) -> Outcome | None:
    """Remove a stale <path>.partial when it is empty or a clone of `repo`
    a crash interrupted; anything else there blocks."""
    if not partial.exists():
        return None
    if any(partial.iterdir()) and not _partial_clone_of(r, partial, repo):
        return blocked(f"{partial} holds something other than a partial clone of "
                       f"{repo}: move it away, then resume")
    shutil.rmtree(partial)
    return None


def _partial_clone_of(r: Run, partial: Path, repo: str) -> bool:
    if not (partial / ".git").exists():
        return False
    try:
        return _origin(r, partial) == repo.lower()
    except subprocess.CalledProcessError:
        return False


def _origin(r: Run, path: Path) -> str:
    """The lowercased owner/name of the clone at `path`'s origin."""
    url = r.sh(["git", "-C", str(path), "remote", "get-url", "origin"])
    return (slug(url) or url.strip()).lower()


def _resource(rec: Record, kind: str, ref: str) -> None:
    entry = {"kind": kind, "ref": ref, "state": "in use"}
    if entry not in rec.resources:
        rec.resources.append(entry)


# -- confirm ------------------------------------------------------------------


def _confirm_step(r: Run, rec: Record) -> Outcome:
    if rec.revision and rec.confirmed_revision == rec.revision:
        return VERIFIED
    req = rec.request
    return blocked(f"review the proposal, then confirm revision {rec.revision[:12]}: "
                   f"agent-ops project add {rec.project} {' '.join(req['repos'])} "
                   f"--board {req['board']['owner']}/{req['board']['number']}")


# -- config-pr ----------------------------------------------------------------

# Temporary: the record's layout is private to this module, but abandoning
# an onboarding takes deleting it by hand until `abandon` ships.
ABANDON = ("abandon the onboarding: delete its record in state_dir/onboarding/ and "
           "close its infra PR by hand, for now, until `agent-ops project abandon` ships")

NO_INFRA_TOKEN = blocked("GH_INFRA_TOKEN is unset: agent-ops-onboard loads it from "
                         "its op.env entry; run project add through that unit")


def _infra_env() -> dict | None:
    """The env of every agent-ops-infra call: GH_TOKEN is the infra token,
    which only the onboarding unit loads. None without it."""
    token = os.environ.get("GH_INFRA_TOKEN")
    return {**os.environ, "GH_TOKEN": token} if token else None


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
    _git(r, env, "fetch", "origin", "--prune")
    return _git(r, env, "show", f"origin/main:{INFRA_TARGETS}")


def _on_main(text: str, project: str) -> bool:
    return any(p.get("name") == project
               for p in yaml.safe_load(text).get("projects") or [])


class OpenPr(NamedTuple):
    number: int
    url: str
    head: str


def _open_pr(r: Run, project: str, env: dict) -> OpenPr | None:
    rows = json.loads(r.sh(["gh", "pr", "list", "--repo", INFRA_REPO, "--head",
                            f"onboard/{project}", "--state", "open",
                            "--json", "number,url,headRefOid"], env=env))
    return OpenPr(rows[0]["number"], rows[0]["url"], rows[0]["headRefOid"]) if rows else None


def _config_pr(r: Run, rec: Record) -> Outcome:
    if not (env := _infra_env()):
        return NO_INFRA_TOKEN
    main = _infra_main(r, env)
    if _on_main(main, rec.project):
        return Outcome(StepState.VERIFIED, "already on infra main", rec.pr_url)
    pr = _open_pr(r, rec.project, env)
    if pr and _pr_current(r, rec, pr, env):
        return Outcome(StepState.VERIFIED, link=rec.pr_url)
    return _push_pr(r, rec, main, pr, env)


def _pr_current(r: Run, rec: Record, pr: OpenPr, env: dict) -> bool:
    """The open PR is the record's push of its revision onto today's main."""
    ip = rec.infra_pr
    return ip is not None and (pr.head, ip.revision, ip.base) == (
        ip.head, rec.revision, _git(r, env, "rev-parse", "origin/main").strip())


def _push_pr(r: Run, rec: Record, main: str, pr: OpenPr | None, env: dict) -> Outcome:
    """Push the branch, then open its PR unless one is open."""
    pushed = _push(r, rec, main, env, pr.url if pr else rec.pr_url)
    if isinstance(pushed, Outcome):
        return pushed
    number, url = (pr.number, pr.url) if pr else _create_pr(r, rec, env)
    rec.infra_pr = InfraPr(number, url, *pushed, rec.revision, r.now.isoformat())
    _resource(rec, "infra-pr", url)
    return Outcome(StepState.VERIFIED, link=url)


AUTHOR = "agent-ops-onboard"


def _push(r: Run, rec: Record, main: str, env: dict,
          link: str) -> tuple[str, str] | Outcome:
    """onboard/<project> rebuilt on infra main and pushed over the head the
    record names (a lease), never over anyone else's. Its head and base.
    A head the record doesn't name is adopted when it is this very commit,
    pushed before a crash lost the record's save (#210: a crash between
    create and record still finds the resource); otherwise it blocks, with
    `link`."""
    branch = f"onboard/{rec.project}"
    built = _commit(r, rec, main, env)
    if isinstance(built, Outcome):
        return built
    head, base, tree = built
    ours = rec.infra_pr.head if rec.infra_pr else ""
    theirs = _git(r, env, "for-each-ref", "--format=%(objectname)",
                  f"refs/remotes/origin/{branch}").strip()
    if theirs != ours:
        if _git(r, env, "show", "-s", "--format=%P %T", theirs).split() == [base, tree]:
            return theirs, base
        return blocked(f"{branch} on {INFRA_REPO} is at {theirs}, which this onboarding "
                       f"didn't push (it pushed {ours or 'nothing yet'}): review it, then "
                       f"delete the branch, or {ABANDON}", link)
    _git(r, env, "push", *([f"--force-with-lease={branch}:{ours}"] if ours else []),
         "origin", f"HEAD:refs/heads/{branch}")
    return head, base


def _commit(r: Run, rec: Record, main: str, env: dict) -> tuple[str, str, str] | Outcome:
    """The project added to infra main's targets.yaml, checked with the
    loader and committed on onboard/<project> in the scratch clone: its
    sha, parent and tree."""
    text = add_project_text(main, yaml.safe_dump([rec.proposal], sort_keys=False))
    try:
        config.parse_config(yaml.safe_load(text))
    except ValueError as e:
        return blocked(f"infra's targets.yaml refuses the project: {e}")
    _git(r, env, "checkout", "-B", f"onboard/{rec.project}", "origin/main")
    (_scratch(r) / INFRA_TARGETS).write_text(text)
    _git(r, env, "add", INFRA_TARGETS)
    _git(r, env, "-c", f"user.name={AUTHOR}", "-c",
         f"user.email={AUTHOR}@users.noreply.github.com",
         "commit", "-m", f"feat(config): onboard {rec.project}")
    return (_git(r, env, "rev-parse", "HEAD").strip(),
            _git(r, env, "rev-parse", "origin/main").strip(),
            _git(r, env, "show", "-s", "--format=%T", "HEAD").strip())


def _create_pr(r: Run, rec: Record, env: dict) -> tuple[int, str]:
    url = r.sh(["gh", "pr", "create", "--repo", INFRA_REPO, "--base", "main",
                "--head", f"onboard/{rec.project}",
                "--title", f"feat(config): onboard {rec.project}",
                "--body", f"Adds project {rec.project}, paused, at proposal "
                          f"revision {rec.revision[:12]}. Opened by agent-ops-onboard, "
                          "which squash-merges it once its checks are green."],
               env=env).strip()
    return int(url.rsplit("/", 1)[1]), url


# -- config-merge -------------------------------------------------------------

CI_LIMIT = timedelta(minutes=30)


def _config_merge(r: Run, rec: Record) -> Outcome:
    """Squash-merge the infra PR once its ci run and every check on its head
    are green (#225): GitHub enforces nothing on agent-ops-infra."""
    if not (env := _infra_env()):
        return NO_INFRA_TOKEN
    ip = rec.infra_pr
    if ip is None:
        return _added_on_main(r, rec, env)
    pr = json.loads(r.sh(["gh", "pr", "view", str(ip.number), "--repo", INFRA_REPO,
                          "--json", "state,mergeCommit,mergedAt"], env=env))
    if pr["state"] == "MERGED":
        return _merged(rec, pr["mergeCommit"]["oid"], pr["mergedAt"])
    if pr["state"] != "OPEN":
        return blocked(f"the infra PR was closed: reopen it, or {ABANDON}", ip.url)
    return _rebuild_or_merge(r, rec, env)


def _merged(rec: Record, sha: str, at: str) -> Outcome:
    rec.merge_sha, rec.merged_at = sha, datetime.fromisoformat(at).isoformat()
    return Outcome(StepState.VERIFIED, link=rec.pr_url)


def _added_on_main(r: Run, rec: Record, env: dict) -> Outcome:
    """config-pr found the project on infra main, so there is no PR: the
    merge is the latest main commit that added or removed its entry, in
    the scratch clone config-pr fetched."""
    found = _git(r, env, "log", "-1", "--format=%H %cI", "-S", f"- name: {rec.project}",
                 "origin/main", "--", INFRA_TARGETS).split()
    if not found:
        return blocked(f"infra main lists {rec.project}, but no commit there adds "
                       f"'- name: {rec.project}' to {INFRA_TARGETS}")
    return _merged(rec, *found)


def _rebuild_or_merge(r: Run, rec: Record, env: dict) -> Outcome:
    """Rebuild the PR when infra main moved, else merge it once green."""
    ip = rec.infra_pr
    main = _infra_main(r, env)
    if _git(r, env, "rev-parse", "origin/main").strip() != ip.base:
        pushed = _push(r, rec, main, env, ip.url)
        if isinstance(pushed, Outcome):
            return pushed
        rec.infra_pr = InfraPr(ip.number, ip.url, *pushed, rec.revision, r.now.isoformat())
        return waiting("infra main moved: the PR was rebuilt on it", ip.url)
    return _merge_when_green(r, rec, env)


def _merge_when_green(r: Run, rec: Record, env: dict) -> Outcome:
    ip = rec.infra_pr
    waiting_on = _ci_waiting_on(r, ip.head, env)
    if isinstance(waiting_on, Outcome):
        return waiting_on
    if not waiting_on:
        return _squash(r, rec, env)
    if r.now - datetime.fromisoformat(ip.pushed_at) > CI_LIMIT:
        return blocked(f"{waiting_on}, 30 minutes after the push", ip.url)
    return waiting(waiting_on, ip.url)


class Check(NamedTuple):
    name: str
    conclusion: str    # "" until it finishes
    link: str


def _ci_waiting_on(r: Run, head: str, env: dict) -> str | Outcome:
    """What `head` still waits for; "" once its ci run succeeded and every
    check run and commit status finished green. A red one blocks, with its
    link."""
    ci = _ci_run(r, head, env)
    if ci is None:
        return "no ci run on the head yet"
    checks = [ci, *_check_runs(r, head, env), *_statuses(r, head, env)]
    if red := next((c for c in checks if ci_verdict(c.conclusion) == "red"), None):
        return blocked(f"infra check {red.name} is {red.conclusion}", red.link)
    pending = [c.name for c in checks if ci_verdict(c.conclusion) != "green"]
    return f"checks pending: {', '.join(pending)}" if pending else ""


def _api(r: Run, env: dict, path: str) -> dict:
    return json.loads(r.sh(["gh", "api", f"repos/{INFRA_REPO}/{path}"], env=env))


def _ci_run(r: Run, head: str, env: dict) -> Check | None:
    """The latest ci workflow run on `head`; only a success is green."""
    runs = [x for x in _api(r, env, f"actions/runs?head_sha={head}&per_page=100")[
        "workflow_runs"] if x["name"] == "ci"]
    if not runs:
        return None
    x = max(runs, key=lambda x: x["id"])
    done = x["conclusion"] if x["status"] == "completed" else ""
    # skipped and neutral are green for a check, not for the ci run
    return Check("ci", done if done == "success" or ci_verdict(done) != "green" else "",
                 x["html_url"])


def _check_runs(r: Run, head: str, env: dict) -> list[Check]:
    return [Check(c["name"], c["conclusion"] if c["status"] == "completed" else "",
                  c["html_url"])
            for c in _api(r, env, f"commits/{head}/check-runs?per_page=100")["check_runs"]]


def _statuses(r: Run, head: str, env: dict) -> list[Check]:
    return [Check(s["context"], "" if s["state"] == "pending" else s["state"],
                  s["target_url"] or "")
            for s in _api(r, env, f"commits/{head}/status")["statuses"]]


def _squash(r: Run, rec: Record, env: dict) -> Outcome:
    """Squash-merge the head into main. The PR's base is read just before:
    one retargeted elsewhere blocks."""
    ip = rec.infra_pr
    base = json.loads(r.sh(["gh", "pr", "view", str(ip.number), "--repo", INFRA_REPO,
                            "--json", "baseRefName"], env=env))["baseRefName"]
    if base != "main":
        return blocked(f"the infra PR now targets {base!r}, not main: set its base "
                       "back to main", ip.url)
    try:
        out = r.sh(["gh", "api", "-X", "PUT", f"repos/{INFRA_REPO}/pulls/{ip.number}/merge",
                    "-f", "merge_method=squash", "-f", f"sha={ip.head}"], env=env)
    except subprocess.CalledProcessError as e:
        return blocked(f"GitHub refused the merge: {(e.stderr or '').strip()}", ip.url)
    return _merged(rec, json.loads(out)["sha"], r.now.isoformat())


# -- deploy -------------------------------------------------------------------

DEPLOY_LIMIT = timedelta(minutes=15)
UPDATER = "agent-ops-update.service"


def _deploy(r: Run, rec: Record) -> Outcome:
    """agent-ops-update deploys the merge (#225): verified once the box's
    infra checkout holds it and the live config loads with the project
    paused."""
    if _box_has(r, rec.merge_sha) and _live_paused(r, rec.project):
        rec.deployed_at = r.now.isoformat()
        return VERIFIED
    merged_at = datetime.fromisoformat(rec.merged_at)
    if journal := _updater_failure(r, merged_at):
        return blocked(f"{UPDATER} failed after the merge:\n{journal}")
    if r.now - merged_at > DEPLOY_LIMIT:
        return blocked("nothing deployed 15 minutes after the merge: is "
                       "agent-ops-update.timer running? "
                       "(systemctl --user status agent-ops-update.timer)")
    return waiting(f"waiting for {UPDATER} to deploy the merge")


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


def _visible(r: Run, rec: Record) -> Outcome:
    if _listed_paused(r, rec.project):
        return VERIFIED
    if r.now - datetime.fromisoformat(rec.deployed_at) > VISIBLE_LIMIT:
        return blocked("the console doesn't list the project 15 minutes after the "
                       "deploy: check agent-ops-web.service "
                       "(systemctl --user status agent-ops-web.service)")
    return waiting("waiting for the console to list the project")


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
