"""The services recorder: one pass turns each unrecorded console change in
`<state_dir>/services-changes.yaml` into a pull request on the infra repo that changes only
`config/services.yaml`, merges it once its `ci` run succeeded, and drops the entry once the
installed `<state_dir>/services.yaml` says the same.

`box` is the path of the live targets.yaml (as for `dispatcher.onboard.run`); `sh` and `get`
are the shell and HTTP seams. A job whose recording cannot be opened or merged is marked
failed in the entry (shown as `change: {state: "failed", pr, url}` by `GET /api/services`)
and is not retried until `POST /api/services/{job}/retry` clears the mark.

The entry's `record` holds what the recorder pushed: `head` and `base` of its commit, `sets`
(the paused value it carries), `pr`, `url`, and `failed` / `red` (the head whose ci was red).
Run on the box as `python -m dispatcher.services_record <targets.yaml>`."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from dispatcher import config, infra_pr, services_changes
from dispatcher.onboard import sh as _sh
from dispatcher.state import state_lock

SERVICES = "config/services.yaml"


class Failed(Exception):
    """A recording that cannot go on; `pr` is the pull request to link, if any."""

    def __init__(self, why: str, pr: infra_pr.OpenPr | None = None):
        super().__init__(why)
        self.pr = pr


def run(box, sh=_sh, get=None) -> None:
    """One recorder pass."""
    state_dir = config.load_config(box).state_dir
    try:
        with state_lock(state_dir, "services-record.lock", 0):
            for job, entry in services_changes.read(state_dir).items():
                if not (entry.get("record") or {}).get("failed"):
                    _record(state_dir, sh, job, entry)
    except TimeoutError:
        return  # another pass is running


def _record(state_dir: str, sh, job: str, entry: dict) -> None:
    want = entry.get("paused")
    try:
        if want is None:
            return _edit(state_dir, job, want, lambda e: {})
        _one(state_dir, sh, job, bool(want), entry.get("record") or {})
    except Exception as e:
        pr = e.pr if isinstance(e, Failed) else None
        _edit(state_dir, job, want, lambda ent: {**ent, "record": {
            **(ent.get("record") or {}), "failed": True,
            **({"pr": pr.number, "url": pr.url} if pr else {})}})


def _edit(state_dir: str, job: str, want, change) -> bool:
    """Apply `change` unless the console changed the entry meanwhile; whether it applied."""
    applied = []

    def edit(e: dict) -> dict:
        if e.get("paused") != want:
            return e
        applied.append(True)
        return change(e)
    services_changes.update(state_dir, job, edit)
    return bool(applied)


def _save(state_dir: str, job: str, want: bool, **fields) -> bool:
    return _edit(state_dir, job, want, lambda e: {**e, "record": {**(e.get("record") or {}), **fields}})


def _installed_paused(state_dir: str, job: str) -> bool:
    f = Path(state_dir) / "services.yaml"
    return bool((((yaml.safe_load(f.read_text()) if f.exists() else None) or {}).get(job) or {})
                .get("paused", False))


def _one(state_dir: str, sh, job: str, want: bool, rec: dict) -> None:
    if not (env := infra_pr.infra_env()):
        raise Failed("GH_INFRA_TOKEN is unset")
    scratch = Path(state_dir) / "services-record" / "infra"
    text = infra_pr.fetch_main(sh, scratch, env, SERVICES)
    branch = f"services/{job}"
    pr = infra_pr.open_pr(sh, branch, env)
    if bool(yaml.safe_load(text)[job]["paused"]) == want:
        if pr:
            _close(sh, pr, env)
        # Kept until the box's installed config says the same, or the sync would undo it.
        if _installed_paused(state_dir, job) == want:
            return _edit(state_dir, job, want, lambda e: {})
        # A record stays, so a Resume before the install keeps the entry (and records itself).
        return _edit(state_dir, job, want, lambda e: {"paused": want, "record": {"recorded": True}})
    if pr is None:
        return _open(state_dir, sh, job, want, text, env, rec.get("head", ""), None)
    if pr.head != rec.get("head") or rec.get("pr") != pr.number:
        raise Failed("the pull request is not the one the recorder opened", pr)
    if rec.get("sets") != want:
        _close(sh, pr, env)
        return _open(state_dir, sh, job, want, text, env, "", None)
    files = [f["path"] for f in json.loads(sh(
        ["gh", "pr", "view", str(pr.number), "--repo", infra_pr.INFRA_REPO, "--json", "files"],
        env=env))["files"]]
    if files != [SERVICES]:
        raise Failed(f"the pull request touches {files}", pr)
    base = infra_pr.git(sh, scratch, env, "rev-parse", "origin/main").strip()
    if rec.get("red") == pr.head or rec.get("base") != base:
        return _open(state_dir, sh, job, want, text, env, pr.head, pr)
    waiting = infra_pr.ci_waiting_on(sh, pr.head, env)
    if isinstance(waiting, infra_pr.Check):
        _save(state_dir, job, want, red=pr.head)
        raise Failed(f"{waiting.name} is {waiting.conclusion}", pr)
    if not waiting:
        # Under the console's lock, so a Resume cannot land between this check and the merge.
        with state_lock(state_dir, "services-changes.lock", None):
            if services_changes.read(state_dir).get(job, {}).get("paused") == want:
                sh(["gh", "pr", "merge", str(pr.number), "--repo", infra_pr.INFRA_REPO, "--merge",
                    "--match-head-commit", pr.head], env=env)


def _close(sh, pr: infra_pr.OpenPr, env: dict) -> None:
    sh(["gh", "pr", "close", str(pr.number), "--repo", infra_pr.INFRA_REPO, "--delete-branch"], env=env)


def _open(state_dir, sh, job, want, text, env, lease, pr) -> None:
    """Build the commit on infra main and push it over `lease`; open its pull request
    unless `pr` is already open."""
    scratch = Path(state_dir) / "services-record" / "infra"
    branch = f"services/{job}"
    git = lambda *a: infra_pr.git(sh, scratch, env, *a)  # noqa: E731
    cfg = yaml.safe_load(text)
    cfg[job]["paused"] = want
    git("checkout", "-B", branch, "origin/main")
    (scratch / SERVICES).write_text(yaml.safe_dump(cfg, sort_keys=False))
    git("add", SERVICES)
    git("commit", "-m", f"chore(services): {'pause' if want else 'resume'} {job}")
    head, base = git("rev-parse", "HEAD").strip(), git("rev-parse", "origin/main").strip()
    infra_pr.push_branch(sh, scratch, env, branch, lease)
    # Each save is dropped if the console changed the entry meanwhile: then stop, or close
    # the pull request just opened, so none is left that no entry knows about.
    if not _save(state_dir, job, want, head=head, base=base, sets=want, red=None) or pr:
        return
    number, url = infra_pr.create_pr(
        sh, branch, f"chore(services): {'pause' if want else 'resume'} {job}",
        f"Records the console's {'pause' if want else 'resume'} of {job} in "
        f"{SERVICES}. Opened by the services recorder; it merges once ci succeeds.", env)
    if not _save(state_dir, job, want, pr=number, url=url):
        _close(sh, infra_pr.OpenPr(number, url, head), env)


if __name__ == "__main__":
    run(sys.argv[1])
