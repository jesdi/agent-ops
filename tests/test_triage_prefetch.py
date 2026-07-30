"""Deterministic gh pre-fetch for triage sessions."""
import json
import subprocess

import pytest

from dispatcher.triage_prefetch import (MAX_COMMENT_CHARS, MAX_COMMENTS,
                                        PrefetchError, prefetch)


class FakeRun:
    """Maps a matched substring of the gh argv to canned stdout."""

    def __init__(self, responses):
        self.responses = responses  # list of (match_tokens, rc, stdout)
        self.calls = []

    def __call__(self, args, capture_output=True, text=True, timeout=120):
        self.calls.append(args)
        for tokens, rc, stdout in self.responses:
            if all(t in args for t in tokens):
                return subprocess.CompletedProcess(args, rc, stdout, "")
        raise AssertionError(f"unexpected gh call: {args}")


CURSOR = "2026-07-29T05:30:00+00:00"


def _fake(issues, comments_by_issue=None, labels=(), types_rc=1,
          open_issues=()):
    comments_by_issue = comments_by_issue or {}
    resp = [
        (["issue", "list", "--search"], 0, json.dumps(issues)),
        (["label", "list"], 0, json.dumps(list(labels))),
        (["api"], types_rc, json.dumps([{"name": "Bug"}]) if types_rc == 0 else ""),
        (["issue", "list", "--state", "open", "--limit", "500"], 0,
         json.dumps(list(open_issues))),
    ]
    for num, comments in comments_by_issue.items():
        resp.insert(0, (["issue", "view", str(num)], 0,
                        json.dumps({"comments": comments})))
    return FakeRun(resp)


ISSUE = {"number": 7, "title": "T", "body": "B",
         "author": {"login": "alice"}, "labels": [{"name": "inbox"}]}


def test_prefetch_shape():
    run = _fake([ISSUE], {7: [{"author": {"login": "bob"}, "body": "hi"}]},
                labels=[{"name": "bug", "description": "broken"}],
                open_issues=[{"number": 7, "title": "T"}])
    blob = prefetch("o/r", CURSOR, run=run)
    assert blob["repo"] == "o/r" and blob["cursor"] == CURSOR
    assert blob["issues"][0]["number"] == 7
    assert blob["issues"][0]["author"] == "alice"
    assert blob["issues"][0]["labels"] == ["inbox"]
    assert blob["issues"][0]["comments"] == [{"author": "bob", "body": "hi"}]
    assert blob["labels"] == [{"name": "bug", "description": "broken"}]
    assert blob["open_issues"] == [{"number": 7, "title": "T"}]


def test_prefetch_empty_window_short_circuits():
    run = _fake([])
    blob = prefetch("o/r", CURSOR, run=run)
    assert blob["issues"] == []
    # only the windowed list call happened — no inventory fetches
    assert len(run.calls) == 1


def test_comment_truncation():
    comments = [{"author": {"login": "b"}, "body": "x" * 5000}
                for _ in range(30)]
    run = _fake([ISSUE], {7: comments})
    blob = prefetch("o/r", CURSOR, run=run)
    got = blob["issues"][0]["comments"]
    assert len(got) == MAX_COMMENTS
    assert all(len(c["body"]) <= MAX_COMMENT_CHARS for c in got)


def test_issue_types_degrade_to_empty():
    run = _fake([ISSUE], {7: []}, types_rc=1)
    assert prefetch("o/r", CURSOR, run=run)["issue_types"] == []


def test_gh_failure_raises():
    run = FakeRun([(["issue", "list", "--search"], 1, "")])
    with pytest.raises(PrefetchError):
        prefetch("o/r", CURSOR, run=run)
