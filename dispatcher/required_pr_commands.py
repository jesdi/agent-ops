"""Explicit operator changes to a required PR set, delivered as durable intents.

Terminal usage (the revision is shown in the task state):
python -m dispatcher.required_pr_commands --state-dir STATE --target shop \
    --repo example/frontend --issue 12 --revision 2 replace-pr example/frontend#9 10
python -m dispatcher.required_pr_commands --state-dir STATE --target shop \
    --repo example/frontend --issue 12 --revision 2 drop-pr example/frontend#9
python -m dispatcher.required_pr_commands --state-dir STATE --target shop \
    --repo example/frontend --issue 12 --revision 2 depend-pr example/frontend#9 example/backend#7
(depend-pr replaces the PR's prerequisites; none given clears them.)
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
    return state.RequiredPR(pr.repo, number, depends_on=pr.depends_on)


def _ref(pr: state.RequiredPR) -> str:
    return f"{pr.repo}#{pr.number}"


def _depends(pr: state.RequiredPR, refs) -> tuple[str, ...]:
    """Edges are only recorded when well formed; an edge to a PR outside the
    required set stays visible as a validation error, not a silent drop."""
    if not isinstance(refs, list) or not all(
            isinstance(r, str) and r.rpartition("#")[2].isdecimal() and "/" in r.rpartition("#")[0]
            for r in refs):
        raise ValueError("prerequisites must be owner/repository#number references")
    return tuple(dict.fromkeys(refs))


def _replace(github, target, task, pr, payload):
    new = _replacement(github, target, pr, payload.get("replacement"))
    old, fresh = _ref(pr), _ref(new)
    prs = tuple(new if p == pr else replace(
        p, depends_on=tuple(fresh if d == old else d for d in p.depends_on))
        for p in task.required_prs)
    return prs, task.dropped_pr_repos


def _drop(task, pr):
    if len(task.required_prs) == 1:
        raise ValueError("cannot drop the last required PR; cancel the task instead")
    return tuple(p for p in task.required_prs if p != pr), (*task.dropped_pr_repos, pr.repo)


def change(github, target, task: state.TaskState, action: str, payload: dict) -> state.TaskState:
    """Validate operator intent against the current revision; never infer a drop."""
    pr = _selected(task, payload)
    revision = task.required_set_revision + 1
    if action == "depend-pr":
        edges = _depends(pr, payload.get("depends_on"))
        prs = tuple(replace(p, depends_on=edges) if p == pr else p for p in task.required_prs)
        return replace(task, required_prs=prs, required_set_revision=revision)
    if action == "replace-pr":
        prs, dropped = _replace(github, target, task, pr, payload)
    elif action == "drop-pr":
        prs, dropped = _drop(task, pr)
    else:
        raise ValueError("unknown required-PR command")
    return replace(task, required_prs=prs, dropped_pr_repos=dropped,
                   required_set_revision=revision,
                   feedback_pending=False, repair_prs=(), feedback_pr="")


def _payload(parser, args) -> dict:
    repo, sep, number = args.pr.rpartition("#")
    if not sep or not number.isdecimal():
        parser.error("PR must be owner/repository#number")
    payload = {"pr_repo": repo, "number": int(number), "replacement": 0, "revision": args.revision}
    if args.action == "replace-pr":
        if not (len(args.extra) == 1 and args.extra[0].isdecimal()):
            parser.error("replace-pr takes one replacement PR number")
        payload["replacement"] = int(args.extra[0])
    elif args.action == "depend-pr":
        payload["depends_on"] = args.extra
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--repo", required=True, help="origin issue repository")
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("action", choices=("replace-pr", "drop-pr", "depend-pr"))
    parser.add_argument("pr", help="required PR as owner/repository#number")
    parser.add_argument("extra", nargs="*", help="replacement number, or prerequisite PRs")
    args = parser.parse_args()
    path = intents.write_intent(
        args.state_dir, args.action, args.target, args.issue,
        _payload(parser, args), "operator", time.time_ns() // 1_000_000, repo=args.repo)
    print(f"Queued {args.action}: {path}")


if __name__ == "__main__":
    main()
