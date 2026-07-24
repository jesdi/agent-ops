"""Turn a failure into exactly one report (GitHub issue + Telegram ping).

Three classes: provisioning and pass-crash file on cfg.infra_repo
(dispatcher-side bugs a session container cannot fix); session-crash files
on the target repo, where a native `Blocked by:` body line is free.
Fingerprint files under state_dir/failures/ dedupe; quarantine records
under state_dir/quarantine/ stop a provisioning-failed task from being
re-claimed until its blocker issue closes."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class FailureReport:
    klass: str     # "provisioning" | "session-crash" | "pass-crash"
    target: str    # target name, "" for pass crashes
    issue: int     # task issue number, 0 for pass crashes
    title: str     # human summary; used in the issue title and Telegram ping
    error: str     # exception text / traceback
    log_tail: str  # last ~30 lines of setup.log / tmux tail, "" if none
    repro: str     # exact command that reproduces the failure
    worktree: str  # path or ""


def _error_signature(error: str) -> str:
    for line in reversed(error.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def fingerprint(report: FailureReport) -> str:
    key = f"{report.klass}|{report.issue}|{_error_signature(report.error)}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def issue_body(report: FailureReport, task_url: str, when: str) -> str:
    return (
        "🤖 agent-ops failure report\n"
        f"- class: {report.klass}\n"
        f"- task: {task_url or '(none)'}\n"
        f"- when: {when}\n"
        f"- worktree: {report.worktree or '(none)'}\n"
        f"- repro: `{report.repro}`\n"
        "\n## Error\n"
        f"```\n{report.error.strip()}\n```\n"
        "\n## Log tail\n"
        f"```\n{report.log_tail.strip() or '(none)'}\n```\n"
        "\nClosing this issue unblocks the task.\n"
    )
