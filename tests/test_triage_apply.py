"""Validation + application of triage session decisions."""
import subprocess

import pytest

from dispatcher.triage_apply import ApplyError, ApplyResult, apply


class FakeRun:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def __call__(self, args, capture_output=True, text=True, timeout=120):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.rc, "", "")


INV = frozenset({"bug", "enhancement", "documentation", "question",
                 "frontend", "backend", "inbox", "auto", "human-required"})


def _d(**issue):
    return {"issues": [{"number": 7, **issue}]}


def test_labels_applied():
    run = FakeRun()
    res = apply("o/r", _d(add_labels=["bug", "auto"], remove_labels=["inbox"]),
                INV, run=run)
    assert res.labeled == 1 and res.rejected == ()
    assert ["gh", "issue", "edit", "7", "--repo", "o/r",
            "--add-label", "bug", "--add-label", "auto",
            "--remove-label", "inbox"] in run.calls


def test_unknown_label_rejected_not_applied():
    run = FakeRun()
    res = apply("o/r", _d(add_labels=["made-up"]), INV, run=run)
    assert res.labeled == 0
    assert res.rejected and "made-up" in res.rejected[0]
    assert run.calls == []


def test_two_type_labels_rejected():
    run = FakeRun()
    res = apply("o/r", _d(add_labels=["bug", "enhancement"]), INV, run=run)
    assert res.labeled == 0 and res.rejected and run.calls == []


def test_three_area_labels_rejected():
    inv = INV | {"infra"}
    res = apply("o/r", _d(add_labels=["frontend", "backend", "infra"]), inv,
                run=FakeRun())
    assert res.labeled == 0 and res.rejected


def test_comment_posted():
    run = FakeRun()
    res = apply("o/r", _d(comment="need repro steps"), INV, run=run)
    assert res.comments == 1
    assert ["gh", "issue", "comment", "7", "--repo", "o/r",
            "--body", "need repro steps"] in run.calls


def test_close_never_executed_only_suggested():
    run = FakeRun()
    res = apply("o/r", _d(close={"kind": "duplicate", "duplicate_of": 3,
                                 "reason": "same crash"}), INV, run=run)
    assert res.closes == ("close #7 as duplicate of #3 — same crash",)
    assert all("close" not in c for c in run.calls)


def test_not_planned_close_line():
    res = apply("o/r", _d(close={"kind": "not_planned", "reason": "spam"}),
                INV, run=FakeRun())
    assert res.closes == ("close #7 as not planned — spam",)


def test_gh_failure_raises():
    with pytest.raises(ApplyError):
        apply("o/r", _d(add_labels=["bug"]), INV, run=FakeRun(rc=1))


def test_rejection_isolated_per_issue():
    run = FakeRun()
    decisions = {"issues": [
        {"number": 1, "add_labels": ["made-up"]},
        {"number": 2, "add_labels": ["bug"]},
    ]}
    res = apply("o/r", decisions, INV, run=run)
    assert res.labeled == 1 and len(res.rejected) == 1


def test_empty_decisions_noop():
    res = apply("o/r", {"issues": []}, INV, run=FakeRun())
    assert res == ApplyResult(labeled=0, comments=0, closes=(), rejected=())


def test_null_comment_not_posted():
    run = FakeRun()
    res = apply("o/r", _d(comment=None), INV, run=run)
    assert res.comments == 0 and run.calls == []


def test_null_label_lists_are_noop():
    run = FakeRun()
    res = apply("o/r", _d(add_labels=None, remove_labels=None), INV, run=run)
    assert res.labeled == 0 and res.rejected == () and run.calls == []
