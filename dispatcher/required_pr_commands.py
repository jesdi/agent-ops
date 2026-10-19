"""Explicit operator changes to a required PR set, delivered as durable intents.

Terminal usage (the revision is shown in the task state):
python -m dispatcher.required_pr_commands --state-dir STATE --target shop \
    --repo example/frontend --issue 12 --revision 2 replace-pr example/frontend#9 10
python -m dispatcher.required_pr_commands --state-dir STATE --target shop \
    --repo example/frontend --issue 12 --revision 2 drop-pr example/frontend#9
"""
from __future__ import annotations

import argparse
import time
from dataclasses import replace

from dispatcher import intents, state


def _selected(task: state.TaskState, payload: dict) -> state.RequiredPR:
    if task.stage is not state.Stage.PR_OPEN:
        raise ValueError("required PR changes need a task awaiting PR completion")
    if payload.get("revision") != task.required_set_revision:
        raise ValueError("required-set revision changed; refresh before confirming")
    identity = (payload.get("pr_repo"), payload.get("number"))
    pr = next((p for p in task.required_prs if (p.repo, p.number) == identity), None)
    if pr is None:
        raise ValueError("PR is not in the current required set")
    return pr


def _replacement(github, target, pr: state.RequiredPR, number: int) -> state.RequiredPR:
    if pr.state != "closed":
        raise ValueError("only a closed unmerged PR can be replaced")
    if type(number) is not int or number < 1 or number == pr.number:
        raise ValueError("replacement must name a different positive PR number")
    payload = github.pr_view(target, number, repo=pr.repo)
    if payload.get("state") not in ("OPEN", "MERGED"):
        raise ValueError("replacement must be an open or merged PR in the same repository")
    return state.RequiredPR(pr.repo, number)


def change(github, target, task: state.TaskState, action: str, payload: dict) -> state.TaskState:
    """Validate operator intent against the current revision; never infer a drop."""
    pr = _selected(task, payload)
    if action == "replace-pr":
        new = _replacement(github, target, pr, payload.get("replacement"))
        prs = tuple(new if p == pr else p for p in task.required_prs)
        dropped = task.dropped_pr_repos
    elif action == "drop-pr":
        if len(task.required_prs) == 1:
            raise ValueError("cannot drop the last required PR; cancel the task instead")
        prs = tuple(p for p in task.required_prs if p != pr)
        dropped = (*task.dropped_pr_repos, pr.repo)
    else:
        raise ValueError("unknown required-PR command")
    return replace(task, required_prs=prs, dropped_pr_repos=dropped,
                   required_set_revision=task.required_set_revision + 1,
                   feedback_pending=False, repair_prs=(), feedback_pr="")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--repo", required=True, help="origin issue repository")
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("action", choices=("replace-pr", "drop-pr"))
    parser.add_argument("pr", help="required PR as owner/repository#number")
    parser.add_argument("replacement", type=int, nargs="?", default=0)
    args = parser.parse_args()
    repo, sep, number = args.pr.rpartition("#")
    if not sep or not number.isdecimal():
        parser.error("PR must be owner/repository#number")
    path = intents.write_intent(
        args.state_dir, args.action, args.target, args.issue,
        {"pr_repo": repo, "number": int(number), "replacement": args.replacement,
         "revision": args.revision}, "operator", time.time_ns() // 1_000_000, repo=args.repo)
    print(f"Queued {args.action}: {path}")


if __name__ == "__main__":
    main()
