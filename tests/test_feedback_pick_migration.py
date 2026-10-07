"""Unit slices for ticket 04 of pinned-tracks that the acceptance tests leave
out: the pick key of a stage, and the halves of the read-time migration that
remove the pick, keep it, and change nothing on a second read."""
import json

import pytest

from dispatcher import state
from dispatcher.models import pick_key, stage_pick
from dispatcher.state import Stage, TaskState, load, save

SOL = "openai/gpt-sol@medium"
FABLE = "anthropic/claude-fable-5-1@medium"


def old_shape(tmp_path, stage, picks, **raw_over):
    """A state file with no recorded provider, as before the record existed."""
    save(tmp_path, TaskState(issue=42, target="t", stage=stage, slot=1,
                             worktree="", branch="b", title="", updated_at="u",
                             picks=picks))
    p = state._path(tmp_path, "t", 42)
    raw = json.loads(p.read_text())
    raw.pop("implement_providers", None)
    raw.update(raw_over)
    p.write_text(json.dumps(raw))
    return p


@pytest.mark.parametrize("stage,key", [
    ("address-review", "feedback"), ("implement", "implement"),
    ("queued", "spec"), ("awaiting-spec-review", "spec"), ("review", "review")])
def test_pick_key_of_a_stage(stage, key):
    assert pick_key(stage) == key


def test_address_review_reads_the_feedback_pick_only():
    assert stage_pick({"implement": SOL}, "address-review") == ""
    assert stage_pick({"feedback": FABLE, "implement": SOL},
                      "address-review") == FABLE


def test_old_shape_at_review_drops_the_pick_and_keeps_the_provider(tmp_path):
    old_shape(tmp_path, Stage.REVIEW, {"implement": SOL, "plan": FABLE})
    got = load(tmp_path, "t", 42)
    assert got.picks == {"plan": FABLE}
    assert got.implement_providers == ["openai"]


def test_old_shape_in_implement_keeps_its_pick(tmp_path):
    old_shape(tmp_path, Stage.IMPLEMENT, {"implement": SOL})
    got = load(tmp_path, "t", 42)
    assert got.picks == {"implement": SOL}
    assert got.implement_providers == ["openai"]


def test_old_shape_crashed_in_address_review_moves_the_pick(tmp_path):
    old_shape(tmp_path, Stage.FAILED, {"implement": SOL},
              crashed_stage="address-review")
    assert load(tmp_path, "t", 42).picks == {"feedback": SOL}


def test_old_shape_crashed_in_implement_keeps_its_pick(tmp_path):
    old_shape(tmp_path, Stage.FAILED, {"implement": SOL},
              crashed_stage="implement")
    assert load(tmp_path, "t", 42).picks == {"implement": SOL}


def test_an_existing_feedback_pick_is_not_replaced(tmp_path):
    old_shape(tmp_path, Stage.PR_OPEN, {"implement": SOL, "feedback": FABLE})
    got = load(tmp_path, "t", 42)
    assert got.picks == {"feedback": FABLE}
    assert got.implement_providers == ["openai"]


def test_recorded_providers_leave_the_implement_pick_alone(tmp_path):
    old_shape(tmp_path, Stage.PR_OPEN, {"implement": SOL},
              implement_providers=["anthropic"])
    got = load(tmp_path, "t", 42)
    assert got.picks == {"implement": SOL}
    assert got.implement_providers == ["anthropic"]


@pytest.mark.parametrize("stage", [Stage.PR_OPEN, Stage.REVIEW, Stage.IMPLEMENT])
def test_migration_is_the_same_on_every_read_and_after_a_save(tmp_path, stage):
    p = old_shape(tmp_path, stage, {"implement": SOL})
    before = p.read_text()
    first = load(tmp_path, "t", 42)
    assert load(tmp_path, "t", 42) == first      # a read does not write
    assert p.read_text() == before
    save(tmp_path, first)
    assert load(tmp_path, "t", 42) == first      # nothing left to migrate
