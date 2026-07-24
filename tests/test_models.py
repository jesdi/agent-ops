import pytest

from dispatcher.models import (DEFAULT_MODEL, DEFAULT_POLICY, ModelPolicy,
                               parse_policy, resolve)

# The two rules from the spec, as they will appear in targets.yaml.
RAW = {
    "default": "claude-opus-4-8",
    "rules": [
        {
            "name": "trivial-backend",
            "when": {"effort": {"max": 1}, "labels_exclude": ["frontend"]},
            "use": "claude-sonnet-4-6",
        },
        {
            "name": "frontend-substantial",
            "when": {"effort": {"min": 2}, "labels_include": ["frontend"]},
            "use": {
                "spec": "claude-fable-5",
                "plan": "claude-fable-5",
                "implement": "claude-opus-4-8",
            },
        },
    ],
}


def policy() -> ModelPolicy:
    return parse_policy(RAW)


# -- the two worked rules ------------------------------------------------

def test_trivial_backend_uses_sonnet_for_every_stage():
    p = policy()
    for stage in ("spec", "plan", "implement"):
        assert resolve(p, stage, 1, ["auto"]) == "claude-sonnet-4-6"


def test_frontend_substantial_splits_fable_and_opus_by_stage():
    p = policy()
    labels = ["auto", "frontend"]
    assert resolve(p, "spec", 3, labels) == "claude-fable-5"
    assert resolve(p, "plan", 3, labels) == "claude-fable-5"
    assert resolve(p, "implement", 3, labels) == "claude-opus-4-8"


# -- the gaps all fall through to default --------------------------------

def test_effort_one_with_frontend_falls_through_to_default():
    assert resolve(policy(), "implement", 1, ["frontend"]) == DEFAULT_MODEL


def test_high_effort_without_frontend_falls_through_to_default():
    assert resolve(policy(), "implement", 3, ["auto"]) == DEFAULT_MODEL


def test_unset_effort_never_matches_a_max_constraint():
    # effort is None -> must NOT be downgraded by {max: 1}
    assert resolve(policy(), "implement", None, ["auto"]) == DEFAULT_MODEL


def test_unset_effort_never_matches_a_min_constraint():
    assert resolve(policy(), "spec", None, ["frontend"]) == DEFAULT_MODEL


# -- matcher semantics ---------------------------------------------------

def test_first_matching_rule_wins():
    p = parse_policy({
        "default": "claude-opus-4-8",
        "rules": [
            {"name": "broad", "when": {"effort": {"max": 5}}, "use": "model-a"},
            {"name": "narrow", "when": {"effort": {"max": 1}}, "use": "model-b"},
        ],
    })
    assert resolve(p, "spec", 1, []) == "model-a"


def test_empty_when_is_a_catch_all():
    p = parse_policy({
        "default": "claude-opus-4-8",
        "rules": [{"name": "everything", "when": {}, "use": "model-a"}],
    })
    assert resolve(p, "spec", None, []) == "model-a"


def test_rule_without_when_is_a_catch_all():
    p = parse_policy({
        "default": "claude-opus-4-8",
        "rules": [{"name": "everything", "use": "model-a"}],
    })
    assert resolve(p, "plan", 4, ["frontend"]) == "model-a"


def test_labels_include_requires_every_listed_label():
    p = parse_policy({
        "default": "d",
        "rules": [{"name": "r", "when": {"labels_include": ["frontend", "auto"]},
                   "use": "model-a"}],
    })
    assert resolve(p, "spec", 2, ["frontend", "auto"]) == "model-a"
    assert resolve(p, "spec", 2, ["frontend"]) == "d"


def test_labels_exclude_rejects_on_any_overlap():
    p = parse_policy({
        "default": "d",
        "rules": [{"name": "r", "when": {"labels_exclude": ["frontend", "wip"]},
                   "use": "model-a"}],
    })
    assert resolve(p, "spec", 2, ["auto"]) == "model-a"
    assert resolve(p, "spec", 2, ["auto", "wip"]) == "d"


def test_label_matching_is_case_sensitive():
    p = parse_policy({
        "default": "d",
        "rules": [{"name": "r", "when": {"labels_include": ["frontend"]},
                   "use": "model-a"}],
    })
    assert resolve(p, "spec", 2, ["Frontend"]) == "d"


def test_effort_min_and_max_together_form_a_range():
    p = parse_policy({
        "default": "d",
        "rules": [{"name": "r", "when": {"effort": {"min": 2, "max": 3}},
                   "use": "model-a"}],
    })
    assert resolve(p, "spec", 2, []) == "model-a"
    assert resolve(p, "spec", 3, []) == "model-a"
    assert resolve(p, "spec", 4, []) == "d"
    assert resolve(p, "spec", 1, []) == "d"


def test_use_map_omitting_a_stage_falls_back_to_default_not_later_rules():
    p = parse_policy({
        "default": "claude-opus-4-8",
        "rules": [
            {"name": "partial", "when": {}, "use": {"spec": "model-a"}},
            {"name": "later", "when": {}, "use": "model-b"},
        ],
    })
    assert resolve(p, "spec", 1, []) == "model-a"
    assert resolve(p, "implement", 1, []) == "claude-opus-4-8"


# -- parsing -------------------------------------------------------------

def test_parse_none_yields_opus_default_with_no_rules():
    p = parse_policy(None)
    assert p == DEFAULT_POLICY
    assert p.default == "claude-opus-4-8"
    assert p.rules == ()


def test_parse_empty_dict_yields_opus_default():
    assert parse_policy({}) == DEFAULT_POLICY


def test_parse_keeps_rule_order_and_names():
    p = policy()
    assert [r.name for r in p.rules] == ["trivial-backend", "frontend-substantial"]


def test_parse_rejects_unknown_when_key():
    with pytest.raises(ValueError, match="label_include"):
        parse_policy({"default": "d", "rules": [
            {"name": "r", "when": {"label_include": ["frontend"]}, "use": "m"}]})


def test_parse_rejects_unknown_stage_in_use_map():
    with pytest.raises(ValueError, match="verify"):
        parse_policy({"default": "d", "rules": [
            {"name": "r", "when": {}, "use": {"verify": "m"}}]})


def test_parse_rejects_non_mapping_effort():
    with pytest.raises(ValueError, match="effort"):
        parse_policy({"default": "d", "rules": [
            {"name": "r", "when": {"effort": 1}, "use": "m"}]})


def test_parse_rejects_unknown_effort_bound():
    with pytest.raises(ValueError, match="exactly"):
        parse_policy({"default": "d", "rules": [
            {"name": "r", "when": {"effort": {"lt": 2}}, "use": "m"}]})


def test_parse_rejects_non_int_effort_bound():
    with pytest.raises(ValueError, match="integer"):
        parse_policy({"default": "d", "rules": [
            {"name": "r", "when": {"effort": {"max": "one"}}, "use": "m"}]})


def test_parse_rejects_rule_without_use():
    with pytest.raises(ValueError, match="use"):
        parse_policy({"default": "d", "rules": [{"name": "r", "when": {}}]})


def test_parse_rejects_non_string_default():
    with pytest.raises(ValueError, match="default"):
        parse_policy({"default": ["claude-opus-4-8"], "rules": []})


def test_parse_rejects_non_list_rules():
    with pytest.raises(ValueError, match="rules"):
        parse_policy({"default": "d", "rules": {"name": "r"}})
