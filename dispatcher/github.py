"""GitHub adapter. All access via the gh CLI; GitHub is the state store
and the board is the double-dispatch guard."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass

from dispatcher.config import Target


def _run(args: list[str], cwd: str | None = None,
         env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=120, check=True,
        env=env,
    ).stdout


def _project_env() -> dict[str, str] | None:
    """User-owned Projects v2 are invisible to fine-grained PATs, so `gh
    project` runs with GH_TOKEN set to the classic project-scope token
    (GH_PROJECT_TOKEN); every other gh call keeps the stored fine-grained
    auth."""
    token = os.environ.get("GH_PROJECT_TOKEN")
    return {**os.environ, "GH_TOKEN": token} if token else None


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    url: str
    effort: int | None = None
    labels: tuple[str, ...] = ()


class GitHubClient:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._project_node_ids: dict[str, str] = {}
        self._viewer_login: str | None = None

    # -- read side ---------------------------------------------------------

    def viewer_login(self) -> str:
        """The box's own GitHub login — poll classification excludes its
        comments. Cached per process; a failed fetch reads as "" (matches
        no author) and is retried on the next call rather than cached."""
        if self._viewer_login is None:
            try:
                self._viewer_login = _run(
                    ["gh", "api", "user", "--jq", ".login"]).strip()
            except (subprocess.CalledProcessError, OSError):
                return ""
        return self._viewer_login

    def pr_view(self, target: Target, pr_number: int) -> dict:
        out = _run(["gh", "pr", "view", str(pr_number), "--repo", target.repo,
                    "--json",
                    "state,mergedAt,reviewDecision,reviews,comments"])
        return json.loads(out)

    def pr_number_for_branch(self, target: Target, branch: str) -> int:
        """The task's PR for `branch`, resolved deterministically: `gh pr
        list` gives no ordering guarantee, so a branch that had an earlier
        closed PR and a newer open one would otherwise leave gh to decide
        which PR the dispatcher watches forever. Prefer the open one; among
        equals take the highest number (the most recent)."""
        out = _run(["gh", "pr", "list", "--repo", target.repo,
                    "--head", branch, "--state", "all",
                    "--json", "number,state", "--limit", "50"])
        rows = json.loads(out)
        if not rows:
            return 0
        best = max(rows, key=lambda r: ((r.get("state") or "").upper() == "OPEN",
                                        r.get("number") or 0))
        return best["number"]

    def run_status(self, target: Target, run_id: int) -> str:
        out = _run(["gh", "run", "view", str(run_id), "--repo", target.repo,
                    "--json", "status,conclusion"])
        d = json.loads(out)
        return (d.get("conclusion") or "") if d.get("status") == "completed" else ""

    def rank_rows(self, target: Target) -> list[dict]:
        """rank_cmd --json rows in rank order; `boost` normalized to an int
        (old vendored skills may not emit it yet, and the board can hand back
        a string or an empty value for an unset field)."""
        out = _run(shlex.split(target.rank_cmd), cwd=target.clone_path)
        rows = json.loads(out)
        for row in rows:
            row["boost"] = int(row.get("boost") or 0)
        return rows

    def candidates(self, target: Target) -> list[Candidate]:
        return [
            Candidate(number=r["number"], title=r["title"], url=r["url"],
                      effort=r.get("effort"), labels=tuple(r.get("labels") or ()))
            for r in self.rank_rows(target)
            if r["status"] == "Ready" and not r["blocked"] and "auto" in r["labels"]
        ]

    def _project_node_id(self, target: Target) -> str:
        key = f"{target.project_owner}/{target.project_number}"
        if key not in self._project_node_ids:
            out = _run(["gh", "project", "view", str(target.project_number),
                        "--owner", target.project_owner, "--format", "json"],
                       env=_project_env())
            self._project_node_ids[key] = json.loads(out)["id"]
        return self._project_node_ids[key]

    def _item_id(self, target: Target, issue: int) -> str:
        out = _run(["gh", "project", "item-list", str(target.project_number),
                    "--owner", target.project_owner, "--format", "json",
                    "--limit", "200"], env=_project_env())
        items = json.loads(out)["items"]
        for item in items:
            if (item.get("content") or {}).get("number") == issue:
                return item["id"]
        # The project-scope token cannot expand linked issues of a private
        # repo (no "content" in item-list). GitHub syncs linked-item titles
        # to issue titles, so the title — fetched with the stored repo auth —
        # is the join key.
        title = json.loads(_run(["gh", "issue", "view", str(issue),
                                 "--repo", target.repo,
                                 "--json", "title"]))["title"]
        matches = [item["id"] for item in items
                   if not item.get("content") and item.get("title") == title]
        if len(matches) == 1:
            return matches[0]
        raise LookupError(f"issue #{issue} not on project {target.project_number}"
                          f" (title join: {len(matches)} items match {title!r})")

    def issue_view(self, repo: str, number: int) -> dict:
        out = _run(["gh", "issue", "view", str(number), "--repo", repo,
                    "--json", "title,body,url"])
        return json.loads(out)

    def issue_state(self, repo: str, number: int) -> str:
        if self.dry_run:
            print(f"[dry-run] issue_state {repo}#{number} -> OPEN")
            return "OPEN"  # safe default: quarantined tasks stay blocked
        out = _run(["gh", "issue", "view", str(number), "--repo", repo,
                    "--json", "state"])
        return json.loads(out)["state"]

    # -- write side --------------------------------------------------------

    def create_issue(self, repo: str, title: str, body: str) -> int:
        if self.dry_run:
            print(f"[dry-run] create_issue on {repo}: {title}")
            return 0
        out = _run(["gh", "issue", "create", "--repo", repo,
                    "--title", title, "--body", body])
        return int(out.strip().rstrip("/").rsplit("/", 1)[-1])

    def set_status(self, target: Target, issue: int, option_id: str) -> None:
        if self.dry_run:
            print(f"[dry-run] set_status #{issue} -> {option_id}")
            return
        _run(["gh", "project", "item-edit",
              "--id", self._item_id(target, issue),
              "--project-id", self._project_node_id(target),
              "--field-id", target.status_field_id,
              "--single-select-option-id", option_id], env=_project_env())

    def set_boost(self, target: Target, issue: int, value: int) -> None:
        if self.dry_run:
            print(f"[dry-run] set_boost #{issue} -> {value}")
            return
        _run(["gh", "project", "item-edit",
              "--id", self._item_id(target, issue),
              "--project-id", self._project_node_id(target),
              "--field-id", target.boost_field_id,
              "--number", str(value)], env=_project_env())

    def add_label(self, target: Target, issue: int, label: str) -> None:
        if self.dry_run:
            print(f"[dry-run] add_label #{issue} +{label}")
            return
        _run(["gh", "issue", "edit", str(issue),
              "--repo", target.repo, "--add-label", label])

    def comment(self, target: Target, issue: int, body: str) -> None:
        if self.dry_run:
            print(f"[dry-run] comment #{issue}: {body}")
            return
        _run(["gh", "issue", "comment", str(issue),
              "--repo", target.repo, "--body", body])

    def claim(self, target: Target, cand: Candidate) -> None:
        if self.dry_run:
            print(f"[dry-run] claim #{cand.number} ({cand.title})")
            return
        self.set_status(target, cand.number, target.status_in_progress_option_id)
        self.comment(target, cand.number, "🤖 picked up by agent-ops")

    def release(self, target: Target, issue: int, reason: str) -> None:
        if self.dry_run:
            print(f"[dry-run] release #{issue}: {reason}")
            return
        self.comment(target, issue,
                     f"🤖 agent-ops released this task: {reason}. "
                     f"Worktree preserved for autopsy.")
        self.set_status(target, issue, target.status_ready_option_id)

    def cancel(self, target: Target, issue: int) -> None:
        """Operator won't-do: unlike release(), the card leaves the claim
        pool for good (Wont do, not Ready) and the issue closes as not
        planned."""
        if self.dry_run:
            print(f"[dry-run] cancel #{issue}")
            return
        self.comment(target, issue,
                     "🤖 canceled by operator — moved to Wont do.")
        if target.status_wont_do_option_id:
            self.set_status(target, issue, target.status_wont_do_option_id)
        else:
            print(f"[warn] {target.name}: status_wont_do_option_id unset — "
                  f"board not updated for canceled #{issue}", file=sys.stderr)
        _run(["gh", "issue", "close", str(issue), "--repo", target.repo,
              "--reason", "not planned"])

    def delete_branch(self, target: Target, branch: str) -> None:
        """Best-effort: repos with delete-branch-on-merge already removed
        it, and a 422/404 must not abort the done path."""
        if self.dry_run:
            print(f"[dry-run] delete_branch {target.repo}:{branch}")
            return
        try:
            _run(["gh", "api", "-X", "DELETE",
                  f"repos/{target.repo}/git/refs/heads/{branch}"])
        except (subprocess.CalledProcessError, OSError):
            pass

    def append_blocked_by(self, target: Target, issue: int,
                          blocker: int) -> None:
        if self.dry_run:
            print(f"[dry-run] append_blocked_by #{issue} <- #{blocker}")
            return
        body = json.loads(_run(["gh", "issue", "view", str(issue),
                                "--repo", target.repo,
                                "--json", "body"]))["body"] or ""
        # Idempotent: skip if a Blocked-by line already references this
        # blocker. \b keeps #7 from matching inside #77.
        if re.search(rf"^\s*Blocked by:.*#{blocker}\b", body, re.MULTILINE):
            return
        line = f"Blocked by: #{blocker}"
        new_body = f"{body.rstrip()}\n\n{line}\n" if body.strip() else f"{line}\n"
        _run(["gh", "issue", "edit", str(issue), "--repo", target.repo,
              "--body", new_body])
