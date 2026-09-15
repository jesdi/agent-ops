import pytest

from dispatcher.models import (DEFAULT_MODEL, DEFAULT_POLICY, EFFORTS, STAGES,
                               Entry, ModelPolicy, bare_model_id, parse_entry,
                               parse_policy, policy_stage, split_model_id,
                               track_from_labels, tracks_text)

RAW = {
    "triage": ["anthropic/claude-sonnet-5@medium"],
    "untracked": "standard",
    "tracks": {
        "trivial": {
            "when": "Rote rename, typo, formatting, dependency bump.",
            "spec": ["anthropic/claude-sonnet-5@low"],
            "plan": ["anthropic/claude-sonnet-5@medium"],
            "implement": ["anthropic/claude-sonnet-5@medium", "openai/gpt-luna@high"],
            "review": ["openai/gpt-luna@xhigh", "anthropic/claude-sonnet-5@medium"],
        },
        "standard": {
            "when": "Bounded change with a clear scope after the questionnaire.",
            "spec": ["anthropic/claude-fable-5-1@medium", "openai/gpt-astra@medium"],
            "plan": ["anthropic/claude-fable-5-1@medium", "openai/gpt-astra@medium"],
            "implement": ["anthropic/claude-sonnet-5@medium", "openai/gpt-sol@medium"],
            "review": ["openai/gpt-astra@medium", "anthropic/claude-opus-5@medium"],
        },
        "frontend": {
            "when": "Mostly UI or frontend work: components, styling, layout, client behaviour. Fable first.",
            "spec": ["anthropic/claude-fable-5-1@medium", "openai/gpt-astra@medium"],
            "plan": ["anthropic/claude-fable-5-1@medium", "openai/gpt-astra@medium"],
            "implement": ["anthropic/claude-fable-5-1@medium", "anthropic/claude-opus-5@medium"],
            "review": ["openai/gpt-astra@medium", "anthropic/claude-opus-5@medium"],
        },
        "security": {
            "when": "Touches auth, secrets, permissions. Never below this track.",
            "spec": ["anthropic/claude-fable-5-1@high", "openai/gpt-astra@high"],
            "plan": ["anthropic/claude-fable-5-1@high", "openai/gpt-astra@high"],
            "implement": ["anthropic/claude-opus-5@medium", "openai/gpt-sol@high"],
            "review": ["openai/gpt-astra@high", "anthropic/claude-fable-5-1@high"],
        },
    },
}


def policy() -> ModelPolicy:
    return parse_policy(RAW)


# -- entries ---------------------------------------------------------------

def test_entry_parses_provider_model_and_effort():
    e = parse_entry("openai/gpt-luna@xhigh", "t")
    assert e == Entry("openai", "gpt-luna", "xhigh")
    assert e.model_id == "openai/gpt-luna"
    assert str(e) == "openai/gpt-luna@xhigh"


def test_bare_entry_is_anthropic_with_no_effort():
    e = parse_entry("claude-opus-5", "t")
    assert e == Entry("anthropic", "claude-opus-5", "")
    assert str(e) == "anthropic/claude-opus-5"


@pytest.mark.parametrize("bad", ["", "model with space", "claude@fast", "openai/", "/gpt",
                                 "a/b/c", "claude@", ["x"]])
def test_entry_rejects_unusable_values(bad):
    with pytest.raises(ValueError, match="t:"):
        parse_entry(bad, "t:")


def test_efforts_are_the_cli_set():
    assert EFFORTS == ("low", "medium", "high", "xhigh", "max")


# -- parsing ----------------------------------------------------------------

def test_parse_keeps_track_order_and_stage_lists():
    p = policy()
    assert list(p.tracks) == ["trivial", "standard", "frontend", "security"]
    assert [str(e) for e in p.tracks["standard"].stages["implement"]] == [
        "anthropic/claude-sonnet-5@medium", "openai/gpt-sol@medium"]
    assert p.tracks["security"].when.startswith("Touches auth")
    assert p.untracked == "standard"
    assert [str(e) for e in p.triage] == ["anthropic/claude-sonnet-5@medium"]


def test_model_ids_are_every_entry_deduplicated_in_order():
    ids = policy().model_ids()
    assert ids[0] == "anthropic/claude-sonnet-5"          # triage first
    assert ids.count("anthropic/claude-fable-5-1") == 1
    assert {"openai/gpt-luna", "openai/gpt-sol", "openai/gpt-astra"} <= set(ids)


def test_gate_entry_is_the_untracked_tracks_first_spec_entry():
    assert str(policy().gate_entry()) == "anthropic/claude-fable-5-1@medium"


def test_parse_none_or_empty_yields_the_default_policy():
    assert parse_policy(None) == DEFAULT_POLICY
    assert parse_policy({}) == DEFAULT_POLICY
    assert list(DEFAULT_POLICY.tracks) == ["standard"]
    assert DEFAULT_POLICY.untracked == "standard"
    for stage in STAGES:
        assert [e.model_id for e in DEFAULT_POLICY.tracks["standard"].stages[stage]] == [
            f"anthropic/{DEFAULT_MODEL}"]
    assert DEFAULT_POLICY.gate_entry().model_id == f"anthropic/{DEFAULT_MODEL}"


@pytest.mark.parametrize("old", [{"default": "x"}, {"rules": []}])
def test_parse_rejects_the_old_shape_naming_the_new_keys(old):
    with pytest.raises(ValueError, match="tracks"):
        parse_policy({**RAW, **old})


def test_parse_rejects_unknown_top_level_key():
    with pytest.raises(ValueError, match="triage_model"):
        parse_policy({**RAW, "triage_model": "x"})


def test_parse_rejects_track_missing_a_stage():
    raw = {**RAW, "tracks": {"t": {"when": "w", "spec": ["m"], "plan": ["m"],
                                   "implement": ["m"]}}, "untracked": "t"}
    with pytest.raises(ValueError, match="review"):
        parse_policy(raw)


def test_parse_rejects_track_with_unknown_key():
    raw = {**RAW, "tracks": {"t": {"when": "w", "spec": ["m"], "plan": ["m"],
                                   "implement": ["m"], "review": ["m"],
                                   "verify": ["m"]}}, "untracked": "t"}
    with pytest.raises(ValueError, match="verify"):
        parse_policy(raw)


def test_parse_rejects_empty_stage_list_and_bare_string():
    base = {"when": "w", "spec": ["m"], "plan": ["m"], "implement": ["m"]}
    with pytest.raises(ValueError, match="review"):
        parse_policy({**RAW, "untracked": "t", "tracks": {"t": {**base, "review": []}}})
    with pytest.raises(ValueError, match="review"):
        parse_policy({**RAW, "untracked": "t", "tracks": {"t": {**base, "review": "m"}}})


def test_parse_rejects_track_without_when():
    raw = {**RAW, "tracks": {"t": {"spec": ["m"], "plan": ["m"], "implement": ["m"],
                                   "review": ["m"]}}, "untracked": "t"}
    with pytest.raises(ValueError, match="when"):
        parse_policy(raw)


def test_parse_rejects_untracked_naming_no_track():
    with pytest.raises(ValueError, match="untracked"):
        parse_policy({**RAW, "untracked": "nope"})


def test_parse_rejects_missing_triage_list():
    raw = dict(RAW)
    del raw["triage"]
    with pytest.raises(ValueError, match="triage"):
        parse_policy(raw)


def test_parse_rejects_bad_effort_inside_a_track():
    raw = {**RAW, "tracks": {"t": {"when": "w", "spec": ["m@turbo"], "plan": ["m"],
                                   "implement": ["m"], "review": ["m"]}},
           "untracked": "t"}
    with pytest.raises(ValueError, match="turbo"):
        parse_policy(raw)


@pytest.mark.parametrize("name", ["", "has space", "with:colon"])
def test_parse_rejects_label_unsafe_track_names(name):
    raw = {**RAW, "tracks": {name: RAW["tracks"]["standard"]}, "untracked": name}
    with pytest.raises(ValueError, match="track name"):
        parse_policy(raw)


# -- helpers ----------------------------------------------------------------

def test_policy_stage_maps_runtime_stages():
    assert policy_stage("queued") == "spec"
    assert policy_stage("awaiting-spec-review") == "spec"
    assert policy_stage("address-review") == "implement"
    assert policy_stage("review") == "review"
    assert policy_stage("blocked") == "blocked"


def test_tracks_text_lists_name_and_when_per_line():
    text = tracks_text(policy())
    assert text.splitlines()[0] == "- `trivial`: Rote rename, typo, formatting, dependency bump."
    assert len(text.splitlines()) == 4


def test_track_from_labels_takes_the_first_configured_track_label():
    p = policy()
    assert track_from_labels(["auto", "track:security"], p) == "security"
    assert track_from_labels(["track:nope", "track:trivial"], p) == "trivial"
    assert track_from_labels(["auto"], p) == "standard"
    assert track_from_labels([], p) == "standard"


# -- provider/model split (unchanged) -----------------------------------------

def test_bare_id_is_anthropic():
    assert split_model_id("claude-sonnet-5") == ("anthropic", "claude-sonnet-5")


def test_prefixed_id_names_its_provider():
    assert split_model_id("openai/gpt-5.4-codex") == ("openai", "gpt-5.4-codex")
    assert bare_model_id("nvidia/claude-sonnet-5") == "claude-sonnet-5"


@pytest.mark.parametrize("bad", ["openai/", "/gpt", "a/b/c"])
def test_malformed_prefix_is_rejected(bad):
    with pytest.raises(ValueError, match="model id"):
        split_model_id(bad)
