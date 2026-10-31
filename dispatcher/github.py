"""GitHub adapter. All access via the gh CLI; GitHub is the state store
and the board is the double-dispatch guard."""
from __future__ import annotations

import json
import re
import logging
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from dispatcher import board, ranking
from dispatcher.config import Repository
from dispatcher.pr_poll import CIStatus

log = logging.getLogger(__name__)


def _run(args: list[str], cwd: str | None = None,
         env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=120, check=True,
        env=env,
    ).stdout


_project_env = board.project_env


def _board_gh(args: list[str]) -> str:
    """One gh call with the project token: every board read and write."""
    return _run(["gh"] + args, env=_project_env())


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    url: str
    effort: int | None = None
    labels: tuple[str, ...] = ()
    repository: str = ""  # the member repository the issue lives in


_ISSUES_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) { issues(first: 100, after: $endCursor) {
    pageInfo { hasNextPage endCursor }
    nodes { number title url body state labels(first: 100) { nodes { name } } }
  } }
}
"""

_ISSUE_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/issues/\d+/?")


def _repo_of(url: object) -> str | None:
    """The lowercased "owner/name" of a GitHub issue url, or None."""
    m = _ISSUE_URL.fullmatch(url) if isinstance(url, str) else None
    return m.group(1).lower() if m else None


class GitHubClient:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
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

    def pr_view(self, repository: Repository, pr_number: int) -> dict:
        out = _run(["gh", "pr", "view", str(pr_number), "--repo", repository.repo,
                    "--json",
                    "state,mergedAt,reviewDecision,reviews,comments,mergeable,headRefOid,headRefName"])
        return json.loads(out)

    def ci_statuses(self, repository: Repository, head: str, branch: str) -> list[CIStatus]:
        """PAT-compatible CI reads, isolated from PR lifecycle reads.

        Each source can fail independently; unreadable CI is warned about,
        never taken as evidence of success or allowed to hide a merge.
        """
        results: list[CIStatus] = []
        if not head:
            return results
        for source in (self._workflow_statuses, self._commit_statuses):
            try:
                results.extend(source(repository, head, branch))
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                log.warning("CI poll failed (%s) for %s at %s: %s",
                            source.__name__, repository.repo, head, detail.strip())
        return results

    def _workflow_statuses(self, repository: Repository, head: str,
                           branch: str) -> list[CIStatus]:
        args = ["gh", "api", f"repos/{repository.repo}/actions/runs",
                "--method", "GET", "--paginate", "--slurp",
                "-f", f"head_sha={head}", "-f", "per_page=100"]
        if branch:
            args += ["-f", f"branch={branch}"]
        pages = json.loads(_run(args))
        latest: dict[tuple[int, str, str], dict] = {}
        for page in pages:
            for run in page["workflow_runs"]:
                if run["head_sha"] != head or (branch and run["head_branch"] != branch):
                    continue
                # A workflow may run for both push and pull_request. Keep
                # them separate, but let a newer run supersede an old red one
                # even when the newer run is still queued or in progress.
                key = (run["workflow_id"], run["event"], run["head_branch"])
                order = (run["run_number"], run.get("run_attempt", 1), run["id"])
                previous = latest.get(key)
                if previous is None or order > (
                        previous["run_number"], previous.get("run_attempt", 1), previous["id"]):
                    latest[key] = run
        return [CIStatus(run.get("conclusion") or "", run.get("updated_at") or "")
                for run in latest.values() if run["status"] == "completed"]

    def _commit_statuses(self, repository: Repository, head: str,
                         branch: str) -> list[CIStatus]:
        pages = json.loads(_run([
            "gh", "api", f"repos/{repository.repo}/commits/{head}/statuses?per_page=100",
            "--paginate", "--slurp"]))
        latest: dict[str, dict] = {}
        for page in pages:
            for status in page:
                context = status["context"]
                if context not in latest or status["id"] > latest[context]["id"]:
                    latest[context] = status
        return [CIStatus(status["state"], status.get("created_at") or "")
                for status in latest.values()]

    def pr_number_for_branch(self, repository: Repository, branch: str) -> int:
        """The task's PR for `branch`, resolved deterministically: `gh pr
        list` gives no ordering guarantee, so a branch that had an earlier
        closed PR and a newer open one would otherwise leave gh to decide
        which PR the dispatcher watches forever. Prefer the open one; among
        equals take the highest number (the most recent)."""
        out = _run(["gh", "pr", "list", "--repo", repository.repo,
                    "--head", branch, "--state", "all",
                    "--json", "number,state", "--limit", "50"])
        rows = json.loads(out)
        if not rows:
            return 0
        best = max(rows, key=lambda r: ((r.get("state") or "").upper() == "OPEN",
                                        r.get("number") or 0))
        return best["number"]

    def run_status(self, repository: Repository, run_id: int) -> str:
        out = _run(["gh", "run", "view", str(run_id), "--repo", repository.repo,
                    "--json", "status,conclusion"])
        d = json.loads(out)
        return (d.get("conclusion") or "") if d.get("status") == "completed" else ""

    def rank_rows(self, repositories: list[Repository]) -> list[dict]:
        """One project's rows in board rank order (see ranking.rank),
        `repositories` being its members in config order: one board read with
        the project token and one issue read per member with the stored auth.
        Each row is tagged `repository` with the member its issue `url` names;
        a row of any other repository is dropped."""
        items = self._board_items(repositories[0])
        issues = [issue for r in repositories for issue in self._issues(r)]
        members = {r.repo.lower(): r.name for r in repositories}
        rows = []
        for row in ranking.rank(items, issues):
            name = members.get(_repo_of(row["url"]))
            if name is not None:
                rows.append({**row, "repository": name})
        return rows

    def _issues(self, repository: Repository) -> list[dict]:
        """Every issue of `repository`, open and closed, as ranking.rank
        reads them. GraphQL pages, so no count cap."""
        owner, name = repository.repo.split("/", 1)
        pages = json.loads(_run([
            "gh", "api", "graphql", "--paginate", "--slurp",
            "-f", f"query={_ISSUES_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"]))
        return [{**node, "repository": repository.repo,
                 "labels": [label["name"] for label in node["labels"]["nodes"]]}
                for page in pages
                for node in page["data"]["repository"]["issues"]["nodes"]]

    def candidates(self, repositories: list[Repository]) -> list[Candidate]:
        """One project's claimable rows (see rank_rows), in rank order across
        its members. A BoardError, before anything is claimed, when the board
        lacks a status a claim writes: In progress, or Ready to release it."""
        with self._live_fields(repositories[0]) as fields:
            for status in ("In progress", "Ready"):
                fields.option("Status", status)
        return [
            Candidate(number=r["number"], title=r["title"], url=r["url"],
                      effort=r.get("effort"), labels=tuple(r.get("labels") or ()),
                      repository=r["repository"])
            for r in self.rank_rows(repositories)
            if r["status"] == "Ready" and not r["blocked"] and "auto" in r["labels"]
        ]

    def _board_items(self, repository: Repository) -> list[board.Item]:
        """Every item on `repository`'s project board, read with the project token."""
        return board.fetch_items(self._fields(repository).id, _board_gh)

    def _fields(self, repository: Repository) -> board.Fields:
        """`repository`'s board: node id, field and option ids by name, read
        live once per process (board.fields)."""
        return board.fields(repository.project.board.owner,
                            repository.project.board.number, _board_gh)

    @contextmanager
    def _live_fields(self, repository: Repository) -> Iterator[board.Fields]:
        """`repository`'s board fields for one write. A failure drops them
        from the cache, so the next attempt reads the live board again: the
        web process lives for days, and an edited option gets a new id."""
        try:
            yield self._fields(repository)
        except Exception:
            board.forget(repository.project.board.owner,
                         repository.project.board.number)
            raise

    def _item_id(self, repository: Repository, issue: int) -> str:
        items = self._board_items(repository)
        for item in items:
            if item.number == issue:
                return item.id
        # The project-scope token cannot expand linked issues of a private
        # repo (the item has no number). GitHub syncs linked-item titles
        # to issue titles, so the title — fetched with the stored repo auth —
        # is the join key.
        title = json.loads(_run(["gh", "issue", "view", str(issue),
                                 "--repo", repository.repo,
                                 "--json", "title"]))["title"]
        matches = [item.id for item in items
                   if item.number is None and item.title == title]
        if len(matches) == 1:
            return matches[0]
        raise LookupError(f"issue #{issue} not on project {repository.project.board.number}"
                          f" (title join: {len(matches)} items match {title!r})")

    def issue_view(self, repo: str, number: int) -> dict:
        out = _run(["gh", "issue", "view", str(number), "--repo", repo,
                    "--json", "title,body,url"])
        return json.loads(out)

    def issue_body(self, repository: Repository, issue: int) -> str:
        return self.issue_view(repository.repo, issue).get("body") or ""

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

    def set_status(self, repository: Repository, issue: int, status: str) -> None:
        """Status `status` (an option name, e.g. "Ready") on the issue's card."""
        if self.dry_run:
            print(f"[dry-run] set_status #{issue} -> {status}")
            return
        with self._live_fields(repository) as fields:
            option = fields.option("Status", status)
            _run(["gh", "project", "item-edit",
                  "--id", self._item_id(repository, issue),
                  "--project-id", fields.id,
                  "--field-id", fields.field("Status"),
                  "--single-select-option-id", option], env=_project_env())

    def set_boost(self, repository: Repository, issue: int, value: int) -> None:
        if self.dry_run:
            print(f"[dry-run] set_boost #{issue} -> {value}")
            return
        with self._live_fields(repository) as fields:
            boost = fields.field("Boost")
            _run(["gh", "project", "item-edit",
                  "--id", self._item_id(repository, issue),
                  "--project-id", fields.id,
                  "--field-id", boost,
                  "--number", str(value)], env=_project_env())

    def add_label(self, repository: Repository, issue: int, label: str) -> None:
        if self.dry_run:
            print(f"[dry-run] add_label #{issue} +{label}")
            return
        _run(["gh", "issue", "edit", str(issue),
              "--repo", repository.repo, "--add-label", label])

    def comment(self, repository: Repository, issue: int, body: str) -> None:
        if self.dry_run:
            print(f"[dry-run] comment #{issue}: {body}")
            return
        _run(["gh", "issue", "comment", str(issue),
              "--repo", repository.repo, "--body", body])

    def close_issue(self, repo: str, number: int) -> None:
        if self.dry_run:
            print(f"[dry-run] close_issue {repo}#{number}")
            return
        _run(["gh", "issue", "close", str(number), "--repo", repo,
              "--reason", "completed"])

    def claim(self, repository: Repository, cand: Candidate) -> None:
        if self.dry_run:
            print(f"[dry-run] claim #{cand.number} ({cand.title})")
            return
        self.set_status(repository, cand.number, "In progress")
        self.comment(repository, cand.number, "🤖 picked up by agent-ops")

    def release(self, repository: Repository, issue: int, reason: str) -> None:
        if self.dry_run:
            print(f"[dry-run] release #{issue}: {reason}")
            return
        self.comment(repository, issue,
                     f"🤖 agent-ops released this task: {reason}. "
                     f"Worktree preserved for autopsy.")
        self.set_status(repository, issue, "Ready")

    def cancel(self, repository: Repository, issue: int) -> None:
        """Operator won't-do: unlike release(), the card leaves the claim
        pool for good (Wont do, not Ready) and the issue closes as not
        planned. The issue closes first: a board without Wont do only skips
        the board move, with a warning."""
        if self.dry_run:
            print(f"[dry-run] cancel #{issue}")
            return
        self.comment(repository, issue,
                     "🤖 canceled by operator — moved to Wont do.")
        _run(["gh", "issue", "close", str(issue), "--repo", repository.repo,
              "--reason", "not planned"])
        try:
            self.set_status(repository, issue, "Wont do")
        except board.BoardError as e:
            log.warning("%s: board not updated for canceled #%d — %s",
                        repository.name, issue, e)

    def delete_branch(self, repository: Repository, branch: str) -> None:
        """Best-effort: repos with delete-branch-on-merge already removed
        it, and a 422/404 must not abort the done path."""
        if self.dry_run:
            print(f"[dry-run] delete_branch {repository.repo}:{branch}")
            return
        try:
            _run(["gh", "api", "-X", "DELETE",
                  f"repos/{repository.repo}/git/refs/heads/{branch}"])
        except (subprocess.CalledProcessError, OSError):
            pass

    def append_blocked_by(self, repository: Repository, issue: int,
                          blocker: int) -> None:
        if self.dry_run:
            print(f"[dry-run] append_blocked_by #{issue} <- #{blocker}")
            return
        body = json.loads(_run(["gh", "issue", "view", str(issue),
                                "--repo", repository.repo,
                                "--json", "body"]))["body"] or ""
        # Idempotent: skip if a Blocked-by line, as ranking reads them,
        # already references this blocker.
        if blocker in ranking.blocked_by(body):
            return
        line = f"Blocked by: #{blocker}"
        new_body = f"{body.rstrip()}\n\n{line}\n" if body.strip() else f"{line}\n"
        _run(["gh", "issue", "edit", str(issue), "--repo", repository.repo,
              "--body", new_body])
