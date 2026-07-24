import json
from pathlib import Path

from dispatcher import failures
from dispatcher.failures import FailureReport


def report(**kw):
    base = dict(klass="provisioning", target="portfolio_eval", issue=192,
                title="provisioning failed: Add widget",
                error="Traceback (most recent call last):\n  ...\n"
                      "RuntimeError: no python 3.13",
                log_tail="line1\nline2",
                repro="podman run --rm agent-ops-session provision",
                worktree="/w/task-192")
    base.update(kw)
    return FailureReport(**base)


def test_fingerprint_stable_across_noise():
    a = report(error="run at 12:00:01 pid 111\nRuntimeError: no python 3.13")
    b = report(error="run at 23:59:59 pid 999\nRuntimeError: no python 3.13\n\n")
    assert failures.fingerprint(a) == failures.fingerprint(b)
    assert len(failures.fingerprint(a)) == 12


def test_fingerprint_distinct_per_class_and_issue():
    base = report()
    assert failures.fingerprint(base) != failures.fingerprint(
        report(klass="pass-crash"))
    assert failures.fingerprint(base) != failures.fingerprint(report(issue=7))


def test_issue_body_format():
    body = failures.issue_body(
        report(), "https://github.com/jesdi/portfolio_eval/issues/192",
        "2026-07-24T12:00:00+00:00")
    assert body.startswith("🤖 agent-ops failure report\n"
                           "- class: provisioning\n")
    assert "- task: https://github.com/jesdi/portfolio_eval/issues/192\n" in body
    assert "- when: 2026-07-24T12:00:00+00:00\n" in body
    assert "- worktree: /w/task-192\n" in body
    assert "- repro: `podman run --rm agent-ops-session provision`\n" in body
    assert "## Error\n```\n" in body and "no python 3.13" in body
    assert "## Log tail\n```\nline1\nline2\n```" in body
    assert body.rstrip().endswith("Closing this issue unblocks the task.")


def test_issue_body_none_placeholders():
    body = failures.issue_body(
        report(issue=0, worktree="", log_tail=""), "", "2026-07-24T12:00:00+00:00")
    assert "- task: (none)\n" in body
    assert "- worktree: (none)\n" in body
    assert "## Log tail\n```\n(none)\n```" in body
