"""Acceptance tests for ticket 01: a track can be marked gate-free.
Black-box through parse_policy only."""
import copy

import pytest

from dispatcher.models import STAGES, parse_policy
from tests.test_models import RAW


def raw_with(**track_keys):
    raw = copy.deepcopy(RAW)
    raw["tracks"]["trivial"].update(track_keys)
    return raw


def test_plan_review_false_loads_as_gate_free():
    assert parse_policy(raw_with(plan_review=False)).tracks["trivial"].plan_review is False


def test_track_without_key_has_the_gate():
    assert parse_policy(copy.deepcopy(RAW)).tracks["trivial"].plan_review is True


def test_plan_review_true_has_the_gate():
    assert parse_policy(raw_with(plan_review=True)).tracks["trivial"].plan_review is True


@pytest.mark.parametrize("bad", ["no", "false", 0, 1, None, [], {}])
def test_non_boolean_plan_review_fails_naming_track_and_key(bad):
    with pytest.raises(ValueError, match=r"(?s)(?=.*trivial)(?=.*plan_review)(?!.*unknown key)"):
        parse_policy(raw_with(plan_review=bad))


def test_existing_track_configs_load_with_same_entries_per_stage():
    policy = parse_policy(copy.deepcopy(RAW))
    assert set(policy.tracks) == set(RAW["tracks"])
    for name, raw_track in RAW["tracks"].items():
        track = policy.tracks[name]
        for stage in STAGES:
            assert [str(e) for e in track.stages[stage]] == raw_track[stage]
