"""Working with the infra repo's pull requests: the token environment, a
scratch clone at origin/main, a leased push, finding or creating a PR, and
the `ci` result for a head. Shared by onboarding and the run recorder. Every
call takes the shell (`sh`), so callers keep their own fakes."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, NamedTuple

from dispatcher.pr_poll import ci_verdict

INFRA_REPO = "jesdi/agent-ops-infra"
INFRA_TARGETS = "config/targets.yaml"
# git authenticates as gh does, so GH_TOKEN in the env picks the token.
GIT_AS_GH = ["git", "-c", "credential.helper=", "-c",
             "credential.helper=!gh auth git-credential"]

Sh = Callable[..., str]


def infra_env() -> dict | None:
    """The env of every agent-ops-infra call: GH_TOKEN is the infra token,
    which only the onboarding unit loads. None without it."""
    token = os.environ.get("GH_INFRA_TOKEN")
    return {**os.environ, "GH_TOKEN": token} if token else None


def git(sh: Sh, scratch: Path, env: dict, *args: str) -> str:
    return sh([*GIT_AS_GH, "-C", str(scratch), *args], env=env)


def fetch_main(sh: Sh, scratch: Path, env: dict) -> str:
    """infra main's targets.yaml, fetched into the scratch clone."""
    if not (scratch / ".git").exists():
        shutil.rmtree(scratch, ignore_errors=True)
        sh([*GIT_AS_GH, "clone", f"https://github.com/{INFRA_REPO}.git", str(scratch)], env=env)
    git(sh, scratch, env, "fetch", "origin", "--prune")
    return git(sh, scratch, env, "show", f"origin/main:{INFRA_TARGETS}")


def push_branch(sh: Sh, scratch: Path, env: dict, branch: str, lease: str) -> None:
    """HEAD over `branch`, leased to `lease` when there is one."""
    git(sh, scratch, env, "push", *([f"--force-with-lease={branch}:{lease}"] if lease else []),
        "origin", f"HEAD:refs/heads/{branch}")


class OpenPr(NamedTuple):
    number: int
    url: str
    head: str


def open_pr(sh: Sh, branch: str, env: dict) -> OpenPr | None:
    rows = json.loads(sh(["gh", "pr", "list", "--repo", INFRA_REPO, "--head", branch,
                          "--state", "open", "--json", "number,url,headRefOid"], env=env))
    return OpenPr(rows[0]["number"], rows[0]["url"], rows[0]["headRefOid"]) if rows else None


def create_pr(sh: Sh, branch: str, title: str, body: str, env: dict) -> tuple[int, str]:
    url = sh(["gh", "pr", "create", "--repo", INFRA_REPO, "--base", "main",
              "--head", branch, "--title", title, "--body", body], env=env).strip()
    return int(url.rsplit("/", 1)[1]), url


class Check(NamedTuple):
    name: str
    conclusion: str    # "" until it finishes
    link: str


def ci_waiting_on(sh: Sh, head: str, env: dict) -> str | Check:
    """What `head` still waits for; "" once its ci run succeeded and every
    other workflow run and commit status finished green. A red check is
    returned as itself."""
    runs = _workflow_runs(sh, head, env)
    if not any(c.name == "ci" for c in runs):
        return "no ci run on the head yet"
    return _blocker([*runs, *_statuses(sh, head, env)])


def _blocker(checks: list[Check]) -> str | Check:
    return _first_red(checks) or _pending(checks)


def _first_red(checks: list[Check]) -> Check | None:
    return next((c for c in checks if ci_verdict(c.conclusion) == "red"), None)


def _pending(checks: list[Check]) -> str:
    pending = [c.name for c in checks if ci_verdict(c.conclusion) != "green"]
    return f"checks pending: {', '.join(pending)}" if pending else ""


def _api(sh: Sh, env: dict, path: str) -> dict:
    return json.loads(sh(["gh", "api", f"repos/{INFRA_REPO}/{path}"], env=env))


def _workflow_runs(sh: Sh, head: str, env: dict) -> list[Check]:
    """The latest run of each workflow on `head`. A fine-grained token cannot
    read check runs, and every infra check is an Actions job, so a run's
    conclusion stands for its jobs."""
    latest: dict[str, dict] = {}
    for x in sorted(_api(sh, env, f"actions/runs?head_sha={head}&per_page=100")[
            "workflow_runs"], key=lambda x: x["id"]):
        latest[x["name"]] = x
    return [_run_check(x) for x in latest.values()]


def _run_check(x: dict) -> Check:
    """A finished run's conclusion; skipped and neutral are green for any
    workflow but ci, which must succeed."""
    done = x["conclusion"] if x["status"] == "completed" else ""
    strict = x["name"] == "ci" and done != "success" and ci_verdict(done) == "green"
    return Check(x["name"], "" if strict else done, x["html_url"])


def _statuses(sh: Sh, head: str, env: dict) -> list[Check]:
    return [Check(s["context"], "" if s["state"] == "pending" else s["state"],
                  s["target_url"] or "")
            for s in _api(sh, env, f"commits/{head}/status")["statuses"]]
