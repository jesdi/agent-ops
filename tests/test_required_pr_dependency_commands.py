import pytest

from dispatcher import required_pr_commands as cmds, state
from dispatcher.state import Stage


class Gh:
    def pr_view(self, target, number, repo=""):
        return {"state": "OPEN"}


def _task():
    return state.TaskState(
        issue=12, target="shop", repo="a/f", slot=0, worktree="", branch="", title="", updated_at=0.0, stage=Stage.PR_OPEN, required_set_revision=2,
        required_prs=(state.RequiredPR("a/f", 9, state="closed", depends_on=("a/b#7",)),
                      state.RequiredPR("a/b", 7)))


def test_depend_pr_records_edges_and_rejects_malformed():
    t = cmds.change(Gh(), None, _task(), "depend-pr",
                    {"revision": 2, "pr_repo": "a/b", "number": 7, "depends_on": ["a/f#9"]})
    assert t.required_prs[1].depends_on == ("a/f#9",)
    with pytest.raises(ValueError):
        cmds.change(Gh(), None, _task(), "depend-pr",
                    {"revision": 2, "pr_repo": "a/b", "number": 7, "depends_on": ["nope"]})


def test_replace_pr_keeps_prerequisites():
    t = cmds.change(Gh(), None, _task(), "replace-pr",
                    {"revision": 2, "pr_repo": "a/f", "number": 9, "replacement": 10})
    assert t.required_prs[0].depends_on == ("a/b#7",)


def test_terminal_depend_pr_queues_intent(tmp_path, monkeypatch):
    import sys
    from dispatcher import intents
    monkeypatch.setattr(sys, "argv", [
        "x", "--state-dir", str(tmp_path), "--target", "shop", "--repo", "a/f", "--issue", "12",
        "--revision", "2", "depend-pr", "a/f#9", "a/b#7"])
    cmds.main()
    (intent,) = intents.list_intents(tmp_path)
    assert intent.payload["depends_on"] == ["a/b#7"]
