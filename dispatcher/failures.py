"""Turn a failure into exactly one report (GitHub issue + Telegram ping).

Three classes: provisioning and pass-crash file on cfg.infra_repo
(dispatcher-side bugs a session container cannot fix); session-crash files
on the target repo, where a native `Blocked by:` body line is free.
Fingerprint files under state_dir/failures/ dedupe; quarantine records
under state_dir/quarantine/ stop a provisioning-failed task from being
re-claimed until its blocker issue closes."""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.config import Config


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint_path(state_dir: str | Path, fp: str) -> Path:
    return Path(state_dir) / "failures" / fp


def reported(state_dir: str | Path, report: FailureReport) -> bool:
    return fingerprint_path(state_dir, fingerprint(report)).exists()


def _route(cfg: Config, report: FailureReport) -> str:
    """Repo to file the issue on; "" = no issue (degrade to ping-only)."""
    if report.klass == "session-crash":
        for t in cfg.targets:
            if t.name == report.target:
                return t.repo
        return ""
    return cfg.infra_repo


def _task_url(cfg: Config, report: FailureReport) -> str:
    if not report.issue:
        return ""
    for t in cfg.targets:
        if t.name == report.target:
            return f"https://github.com/{t.repo}/issues/{report.issue}"
    return ""


def report_failure(cfg: Config, deps, report: FailureReport,
                   dry_run: bool = False) -> int:
    """File an issue + send one Telegram ping, exactly once per fingerprint.

    Returns the blocker issue number (0 if degraded, failed, or dry-run;
    the previously filed number on a dedupe hit). Never raises — a failure
    to report must not break dispatching."""
    try:
        marker = fingerprint_path(cfg.state_dir, fingerprint(report))
        if marker.exists():
            return int(json.loads(marker.read_text()).get("issue", 0))
        if dry_run:
            print(f"[dry-run] report {report.klass} failure: {report.title}")
            return 0
        repo = _route(cfg, report)
        number, url = 0, ""
        if repo:
            number = deps.github.create_issue(
                repo, f"[agent-ops] {report.klass}: {report.title}",
                issue_body(report, _task_url(cfg, report), _now()))
            url = f"https://github.com/{repo}/issues/{number}"
        else:
            print(f"[warn] no repo to file {report.klass} failure on "
                  f"(infra_repo unset?): {report.title}", file=sys.stderr)
        deps.notifier.send("task_failed", issue=report.issue,
                           title=report.title, url=url, note=report.klass)
        # Marker is written LAST: a create_issue outage above leaves no
        # marker, so the next pass retries the report.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(
            {"repo": repo, "issue": number, "when": _now()}))
        return number
    except Exception as exc:
        print(f"[warn] failure reporting itself failed: {exc}",
              file=sys.stderr)
        return 0


def tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:])


def setup_log_tail(worktree: str) -> str:
    p = Path(worktree) / ".agent" / "setup.log"
    try:
        return tail(p.read_text()) if p.exists() else ""
    except OSError:
        return ""


def quarantine_path(state_dir: str | Path, target_name: str,
                    issue: int) -> Path:
    return Path(state_dir) / "quarantine" / f"{target_name}-{issue}.json"


def write_quarantine(state_dir: str | Path, target_name: str, issue: int,
                     blocker_repo: str, blocker_issue: int, fp: str) -> None:
    p = quarantine_path(state_dir, target_name, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "task_issue": issue,
        "blocker_repo": blocker_repo,
        "blocker_issue": blocker_issue,
        "fingerprint": fp,
        "created_at": _now(),
    }, indent=2))


def check_quarantine(state_dir: str | Path, github, target_name: str,
                     issue: int) -> bool:
    """True = still quarantined (skip the candidate). Deleting the record
    file manually force-retries; closing the blocker issue deletes it here,
    making the candidate claimable in the same pass."""
    p = quarantine_path(state_dir, target_name, issue)
    if not p.exists():
        return False
    try:
        rec = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return True  # unreadable record: stay blocked; a human deletes it
    if not rec.get("blocker_issue"):
        print(f"[warn] quarantine {p.name} has no blocker issue; "
              f"delete the record to retry", file=sys.stderr)
        return True
    try:
        state = github.issue_state(rec["blocker_repo"], rec["blocker_issue"])
    except Exception as exc:
        print(f"[warn] issue_state failed for {p.name}: {exc} — "
              f"treating as still blocked", file=sys.stderr)
        return True
    if str(state).upper() == "OPEN":
        return True
    # Blocker closed: clear the fingerprint marker too, or a human closing
    # the issue without fixing the cause silently loops forever — the
    # marker still dedupes, so report_failure files nothing and pings
    # nothing next time the same failure recurs.
    fp = rec.get("fingerprint")
    if fp:
        fingerprint_path(state_dir, fp).unlink(missing_ok=True)
    p.unlink(missing_ok=True)
    return False
