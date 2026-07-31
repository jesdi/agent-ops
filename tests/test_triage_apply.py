"""Validation + application of triage session decisions."""
import subprocess

import pytest

from dispatcher.triage_apply import ApplyError, ApplyResult, _gh, apply


class FakeRun:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def __call__(self, args, capture_output=True, text=True, timeout=120):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.rc, "", "")


class FailingRun(FakeRun):
    """Fails only the gh calls whose argv contains `fail_for`."""

    def __init__(self, fail_for):
        super().__init__()
        self.fail_for = fail_for

    def __call__(self, args, capture_output=True, text=True, timeout=120):
        self.calls.append(args)
        rc = 1 if self.fail_for in args else 0
        return subprocess.CompletedProcess(
            args, rc, "", "no such label\n" if rc else "")


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


def test_gh_failure_recorded_against_its_issue_and_loop_completes():
    """A failing gh call must not abort the repo: aborting left earlier writes
    done but unreported, so the report never said what had been mutated."""
    run = FailingRun(fail_for="1")
    decisions = {"issues": [{"number": 1, "add_labels": ["bug"]},
                            {"number": 2, "add_labels": ["enhancement"]}]}
    res = apply("o/r", decisions, INV, run=run)
    assert res.labeled == 1  # #2 still applied
    assert len(res.rejected) == 1
    assert res.rejected[0].startswith("#1: gh issue edit failed")
    assert "no such label" in res.rejected[0]


def test_partial_writes_for_one_issue_are_still_counted():
    # labels land, the comment call fails: the result must show the label
    run = FailingRun(fail_for="comment")
    res = apply("o/r", _d(add_labels=["bug"], comment="hi"), INV, run=run)
    assert res.labeled == 1 and res.comments == 0
    assert res.rejected[0].startswith("#7: gh issue comment failed")


def test_malformed_entry_isolated_with_its_number():
    run = FakeRun()
    decisions = {"issues": [{"add_labels": ["bug"]},           # no number
                            {"number": "abc"},                 # unparseable
                            "nonsense",                        # not a dict
                            {"number": 3, "add_labels": ["bug"]}]}
    res = apply("o/r", decisions, INV, run=run)
    assert res.labeled == 1  # #3 still applied
    assert len(res.rejected) == 3
    assert res.rejected[0].startswith("#?: malformed decision entry")
    assert all("malformed decision entry" in r for r in res.rejected)


def test_apply_error_names_the_issue():
    with pytest.raises(ApplyError) as exc:
        _gh(FakeRun(rc=1), 7, ["issue", "edit", "7", "--repo", "o/r"])
    assert str(exc.value).startswith("#7: gh issue edit failed")


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
