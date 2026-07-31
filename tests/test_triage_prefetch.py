"""Deterministic gh pre-fetch for triage sessions."""
import json
import re
import subprocess

import pytest

from dispatcher.triage_prefetch import (MAX_BODY_CHARS, MAX_COMMENT_CHARS,
                                        MAX_COMMENTS, MAX_CONTEXT_BYTES,
                                        MAX_OPEN_ISSUES, TRUNC_MARK,
                                        ContextTooLargeError, PrefetchError,
                                        bound, prefetch)


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


def test_search_qualifier_is_a_shape_github_accepts():
    """GitHub's search date grammar takes no fractional seconds, and an
    unparseable date qualifier is not an error — it degrades to free text and
    matches ~nothing, so the sweep would silently report "nothing new" for
    every repo, forever."""
    run = _fake([])
    prefetch("o/r", "2026-07-29T05:30:00Z", run=run)
    [args] = run.calls
    qualifier = args[args.index("--search") + 1]
    assert re.fullmatch(r"updated:>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        qualifier)


def test_bound_truncates_long_bodies_and_says_so():
    blob = {"repo": "o/r", "issues": [{"number": 7, "body": "x" * 9000,
                                       "comments": []}],
            "open_issues": [], "labels": []}
    out = bound(blob)
    body = out["issues"][0]["body"]
    assert len(body) == MAX_BODY_CHARS + len(TRUNC_MARK)
    assert body.endswith(TRUNC_MARK)
    assert any("#7" in n and "body truncated" in n for n in out["truncated"])
    assert blob["issues"][0]["body"] == "x" * 9000  # caller's blob untouched


def test_bound_caps_open_issue_list():
    blob = {"repo": "o/r", "issues": [{"number": 1, "body": "b",
                                       "comments": []}],
            "open_issues": [{"number": n, "title": "t"} for n in range(400)],
            "labels": []}
    out = bound(blob)
    assert len(out["open_issues"]) == MAX_OPEN_ISSUES
    assert any("open_issues" in n for n in out["truncated"])


def _rendered(blob) -> int:
    """Bytes of the blob as it actually reaches the prompt — indent=2, which
    is ~25% bigger than the compact form _size() must therefore not use."""
    return len(json.dumps(blob, indent=2).encode())


def test_bound_sheds_until_under_the_budget_without_dropping_issues():
    issues = [{"number": n, "title": "t", "body": "y" * 3000,
               "comments": [{"author": "a", "body": "c" * 900}
                            for _ in range(20)]}
              for n in range(60)]
    blob = {"repo": "o/r", "issues": issues, "labels": [],
            "open_issues": [{"number": n, "title": "t" * 40}
                            for n in range(500)]}
    out = bound(blob)
    assert _rendered(out) <= MAX_CONTEXT_BYTES
    # every issue in the window survives — the cursor advances past them
    assert [i["number"] for i in out["issues"]] == list(range(60))
    joined = " ".join(out["truncated"])
    assert "open_issues" in joined and "comments" in joined and "bodies" in joined


def _worst_case_window(nlabels):
    """The biggest window prefetch can actually produce: 100 issues (its
    --limit), long titles and bodies, 20 comments each, 500 open issues, and a
    label inventory of nlabels."""
    return {"repo": "o/r", "cursor": CURSOR,
            "issues": [{"number": n, "title": "T" * 250, "body": "b" * 5000,
                        "author": "a", "labels": ["inbox"],
                        "comments": [{"author": "x", "body": "c" * 900}
                                     for _ in range(20)]}
                       for n in range(100)],
            "labels": [{"name": f"label-name-{i}", "description": "d" * 120}
                       for i in range(nlabels)],
            "issue_types": [],
            "open_issues": [{"number": n, "title": "t" * 60}
                            for n in range(500)]}


@pytest.mark.parametrize("nlabels", [200, 400])
def test_bound_fits_a_full_window_with_a_big_label_inventory(nlabels):
    """A 200-label inventory used to leave the rendered blob at 128,811 bytes
    (131,292 in the prompt) — past MAX_ARG_STRLEN, which is #1's failure mode
    one threshold higher: the container's `claude -p "$(cat …)"` fails, the
    cursor never advances, and the same window retries every morning."""
    out = bound(_worst_case_window(nlabels))
    assert _rendered(out) <= MAX_CONTEXT_BYTES
    assert [i["number"] for i in out["issues"]] == list(range(100))
    joined = " ".join(out["truncated"])
    assert "labels" in joined and "titles" in joined


def test_bound_sheds_titles_and_labels_only_when_it_has_to():
    """A typical window (a handful of issues, a normal inventory) must keep its
    titles, label descriptions and comments — the deep sheds are for the
    100-issue worst case only."""
    blob = _worst_case_window(60)
    blob["issues"] = [dict(i, comments=i["comments"][:3])
                      for i in blob["issues"][:6]]
    out = bound(blob)
    joined = " ".join(out["truncated"])
    assert "labels" not in joined and "titles" not in joined
    assert "comments" not in joined
    assert out["issues"][0]["title"] == "T" * 250
    assert out["labels"][0]["description"] == "d" * 120
    assert _rendered(out) <= MAX_CONTEXT_BYTES


def test_bound_raises_rather_than_silently_exceeding_the_budget(monkeypatch):
    """When even a fully stripped window will not fit, the sweep must fail
    diagnosably (per-repo FAILED line, cursor untouched) rather than hand the
    container an argv it cannot exec."""
    monkeypatch.setattr("dispatcher.triage_prefetch.MAX_CONTEXT_BYTES", 200)
    with pytest.raises(ContextTooLargeError) as exc:
        bound(_worst_case_window(50))
    assert "200" in str(exc.value)


def test_bound_leaves_a_small_blob_alone():
    blob = {"repo": "o/r", "cursor": CURSOR,
            "issues": [{"number": 7, "title": "T", "body": "B",
                        "comments": [{"author": "b", "body": "hi"}]}],
            "labels": [{"name": "bug", "description": ""}],
            "issue_types": [], "open_issues": [{"number": 7, "title": "T"}]}
    out = bound(blob)
    assert out["truncated"] == []
    assert {k: v for k, v in out.items() if k != "truncated"} == blob
