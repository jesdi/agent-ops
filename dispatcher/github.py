"""GitHub adapter. All access via the gh CLI; GitHub is the state store
and the board is the double-dispatch guard."""
from __future__ import annotations

import json
import re
import logging
import shlex
import subprocess
from collections import Counter
from dataclasses import dataclass

from dispatcher import board
from dispatcher.config import Project, Target, normalize_repo
from dispatcher.pr_poll import CIStatus

log = logging.getLogger(__name__)


def _run(args: list[str], cwd: str | None = None,
         env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=120, check=True,
        env=env,
    ).stdout


_project_env = board.project_env

_ISSUE_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/issues/\d+$")


def _ambiguous_issue_numbers(verified: dict) -> set[int]:
    """Bare issue numbers shared by multiple verified repository identities."""
    counts = Counter(number for _, number in verified)
    return {number for number, count in counts.items() if count > 1}


def _blocker_numbers(body: str) -> set[int]:
    """Read dependency fields without treating ordinary issue mentions as edges."""
    return {int(n) for line in re.findall(r"^\s*Blocked by:\s*(.+)$", body,
                                          re.IGNORECASE | re.MULTILINE)
            for n in re.findall(r"#(\d+)", line)}


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    url: str
    repo: str = ""
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
                    "state,mergedAt,reviewDecision,reviews,comments,mergeable,headRefOid,headRefName"])
        return json.loads(out)

    def ci_statuses(self, target: Target, head: str, branch: str) -> list[CIStatus]:
        """PAT-compatible CI reads, isolated from PR lifecycle reads.

        Each source can fail independently; unreadable CI is warned about,
        never taken as evidence of success or allowed to hide a merge.
        """
        results: list[CIStatus] = []
        if not head:
            return results
        for source in (self._workflow_statuses, self._commit_statuses):
            try:
                results.extend(source(target, head, branch))
            except (subprocess.SubprocessError, OSError, ValueError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                log.warning("CI poll failed (%s) for %s at %s: %s",
                            source.__name__, target.repo, head, detail.strip())
        return results

    def _workflow_statuses(self, target: Target, head: str,
                           branch: str) -> list[CIStatus]:
        args = ["gh", "api", f"repos/{target.repo}/actions/runs",
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

    def _commit_statuses(self, target: Target, head: str,
                         branch: str) -> list[CIStatus]:
        pages = json.loads(_run([
            "gh", "api", f"repos/{target.repo}/commits/{head}/statuses?per_page=100",
            "--paginate", "--slurp"]))
        latest: dict[str, dict] = {}
        for page in pages:
            for status in page:
                context = status["context"]
                if context not in latest or status["id"] > latest[context]["id"]:
                    latest[context] = status
        return [CIStatus(status["state"], status.get("created_at") or "")
                for status in latest.values()]

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

    def _rank_rows_one(self, repo: str, rank_cmd: str, clone_path: str) -> list[dict]:
        out = _run(shlex.split(rank_cmd), cwd=clone_path)
        rows = json.loads(out)
        for row in rows:
            row["boost"] = int(row.get("boost") or 0)
            match = _ISSUE_URL.match(row.get("url") or "")
            if match is not None and normalize_repo(match.group(1)) != normalize_repo(repo):
                # The row's own URL names a different repository than the
                # one whose rank_cmd produced it (a shared board's title
                # join can resolve to another repo's issue) — this identity
                # is unverifiable, so the row is unavailable rather than
                # assigned to the invoking repo on trust.
                row["number"] = None
            row["repo"] = repo
        return rows

    @staticmethod
    def _rank_sort_key(row: dict) -> tuple:
        """Mirror the backlog skill's rank._sort_key: boost band first,
        then score. rank_cmd's own output already orders same-band,
        same-score rows by its further tiebreaks (e.g. impact), which this
        key does not see — Python's sort is stable, so leaving those rows
        unranked here preserves that order instead of re-deriving it (and
        getting it wrong) from an issue number that isn't a priority
        signal."""
        score = row.get("score")
        return (
            -row.get("boost", 0),
            0 if score is not None else 1,
            -(score or 0.0),
        )

    def _blocked_by_ambiguous(self, row: dict, verified: dict) -> bool:
        """Re-derive blockers the old rank script resolved by bare issue
        number. Without collisions, keep rank's answer. With collisions,
        check every dependency against this repository's own card so clearing
        a false sibling blocker does not clear a genuine local blocker."""
        repo = normalize_repo(row["repo"])
        ambiguous = _ambiguous_issue_numbers(verified) - {row["number"]}
        if not ambiguous:
            return row["blocked"]
        body = self.issue_view(row["repo"], row["number"]).get("body") or ""
        listed = _blocker_numbers(body)
        for number in sorted(listed - {row["number"]}):
            blocker = verified.get((repo, number))
            if blocker is None:
                continue  # dropped edge, as rank drops unknown blockers
            if blocker.status != "Done" and self.issue_state(row["repo"], number) != "CLOSED":
                return True
        return False

    def rank_rows(self, target: Target | Project) -> list[dict]:
        """rank_cmd --json rows, each tagged with its repository identity.
        A `Target` runs its single rank_cmd in rank order. A `Project` runs
        every member repository's rank_cmd and merges the results by the
        same boost/score ranking rules a single repo uses, into one
        project-wide order, rather than concatenating each repo's rows
        after the other — concatenation would let an earlier repo's
        low-priority work starve a later repo's urgent one, and sorting by
        score alone would drop an operator's boost priority."""
        if isinstance(target, Project):
            rows = [row for repo in target.repositories
                    for row in self._rank_rows_one(repo.repo, repo.rank_cmd, repo.clone_path)]
            # Old rank scripts can synthesize this repo's genuine issue URL
            # after a title-only join. Verify against the shared board itself;
            # rank output alone cannot prove which repository owns a card.
            items = board.fetch_items(
                self._project_node_id(target),
                lambda args: _run(["gh"] + args, env=_project_env()))
            verified = {(normalize_repo(item.repo), item.number): item for item in items
                        if item.repo is not None and item.number is not None}
            for row in rows:
                card = verified.get((normalize_repo(row["repo"]), row.get("number")))
                if card is None:
                    log.warning("Unavailable board identity: %s#%s", row["repo"],
                                row.get("number"))
                    row["number"] = None
                    continue
                # The old rank script joined status by title and blockers by
                # bare number, so a sibling card may have supplied them. The
                # verified card is the only source of its own status.
                row["status"] = card.status
                if row["status"] == "Ready":
                    row["blocked"] = self._blocked_by_ambiguous(row, verified)
            rows.sort(key=self._rank_sort_key)
            return rows
        return self._rank_rows_one(target.repo, target.rank_cmd, target.clone_path)

    def candidates(self, target: Target | Project) -> list[Candidate]:
        """Eligible rows become candidates. A row with no verified
        repository-qualified issue number (redacted board content) is
        unavailable — a shared title never stands in for the missing
        identity."""
        return [
            Candidate(repo=r["repo"], number=r["number"], title=r["title"],
                      url=r["url"], effort=r.get("effort"),
                      labels=tuple(r.get("labels") or ()))
            for r in self.rank_rows(target)
            if r.get("number") is not None and r["status"] == "Ready"
            and not r["blocked"] and "auto" in r["labels"]
        ]

    def _project_node_id(self, target: Target | Project) -> str:
        owner, number = ((target.board_owner, target.board_number)
                         if isinstance(target, Project)
                         else (target.project_owner, target.project_number))
        key = f"{owner}/{number}"
        if key not in self._project_node_ids:
            out = _run(["gh", "project", "view", str(number),
                        "--owner", owner, "--format", "json"],
                       env=_project_env())
            self._project_node_ids[key] = json.loads(out)["id"]
        return self._project_node_ids[key]

    def _item_id(self, target: Target, issue: int, repo: str = "") -> str:
        items = board.fetch_items(
            self._project_node_id(target),
            lambda args: _run(["gh"] + args, env=_project_env()))
        # A shared board can carry another repo's card with the same issue
        # number or title. Only an item whose repository is positively
        # confirmed as target.repo is writable: a redacted item (repo or
        # number unknown) is unavailable, and a title match alone never
        # authorizes a board update.
        target_repo = normalize_repo(repo or target.repo)
        for item in items:
            if item.number == issue and item.repo is not None \
                    and normalize_repo(item.repo) == target_repo:
                return item.id
        raise LookupError(f"issue #{issue} not on project {target.project_number}"
                          f" as a verified card in {repo or target.repo}")

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

    def set_status(self, target: Target, issue: int, option_id: str,
                   repo: str = "") -> None:
        if self.dry_run:
            print(f"[dry-run] set_status #{issue} -> {option_id}")
            return
        _run(["gh", "project", "item-edit",
              "--id", self._item_id(target, issue, repo),
              "--project-id", self._project_node_id(target),
              "--field-id", target.status_field_id,
              "--single-select-option-id", option_id], env=_project_env())

    def set_boost(self, target: Target, issue: int, value: int,
                  repo: str = "") -> None:
        if self.dry_run:
            print(f"[dry-run] set_boost #{issue} -> {value}")
            return
        _run(["gh", "project", "item-edit",
              "--id", self._item_id(target, issue, repo),
              "--project-id", self._project_node_id(target),
              "--field-id", target.boost_field_id,
              "--number", str(value)], env=_project_env())

    def add_label(self, target: Target, issue: int, label: str,
                  repo: str = "") -> None:
        if self.dry_run:
            print(f"[dry-run] add_label #{issue} +{label}")
            return
        _run(["gh", "issue", "edit", str(issue),
              "--repo", repo or target.repo, "--add-label", label])

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
            log.warning("%s: status_wont_do_option_id unset — board not "
                        "updated for canceled #%d", target.name, issue)
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
