"""GitHub adapter. All access via the gh CLI; GitHub is the state store
and the board is the double-dispatch guard."""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass

from dispatcher.config import Target


def _run(args: list[str], cwd: str | None = None) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=120, check=True,
    ).stdout


@dataclass(frozen=True)
class Candidate:
    number: int
    title: str
    url: str


class GitHubClient:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._project_node_ids: dict[str, str] = {}

    # -- read side ---------------------------------------------------------

    def run_status(self, target: Target, run_id: int) -> str:
        out = _run(["gh", "run", "view", str(run_id), "--repo", target.repo,
                    "--json", "status,conclusion"])
        d = json.loads(out)
        return (d.get("conclusion") or "") if d.get("status") == "completed" else ""

    def candidates(self, target: Target) -> list[Candidate]:
        out = _run(shlex.split(target.rank_cmd), cwd=target.clone_path)
        items = json.loads(out)
        return [
            Candidate(number=i["number"], title=i["title"], url=i["url"])
            for i in items
            if i["status"] == "Ready" and not i["blocked"] and "auto" in i["labels"]
        ]

    def _project_node_id(self, target: Target) -> str:
        key = f"{target.project_owner}/{target.project_number}"
        if key not in self._project_node_ids:
            out = _run(["gh", "project", "view", str(target.project_number),
                        "--owner", target.project_owner, "--format", "json"])
            self._project_node_ids[key] = json.loads(out)["id"]
        return self._project_node_ids[key]

    def _item_id(self, target: Target, issue: int) -> str:
        out = _run(["gh", "project", "item-list", str(target.project_number),
                    "--owner", target.project_owner, "--format", "json",
                    "--limit", "200"])
        for item in json.loads(out)["items"]:
            if (item.get("content") or {}).get("number") == issue:
                return item["id"]
        raise LookupError(f"issue #{issue} not on project {target.project_number}")

    # -- write side --------------------------------------------------------

    def set_status(self, target: Target, issue: int, option_id: str) -> None:
        if self.dry_run:
            print(f"[dry-run] set_status #{issue} -> {option_id}")
            return
        _run(["gh", "project", "item-edit",
              "--id", self._item_id(target, issue),
              "--project-id", self._project_node_id(target),
              "--field-id", target.status_field_id,
              "--single-select-option-id", option_id])

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
