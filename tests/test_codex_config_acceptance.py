"""Acceptance tests for ticket 03: per-provider effort/model-config validation.

Black-box through the public `models:` policy parser (dispatcher.models) and
config load (dispatcher.config.load_config). See
docs/specs/2026-09-24-codex-runtime-design.md (Effort, Config) and
.agent/tickets/03-per-provider-config-validation.md.

Interface assumed but not yet implemented (per ticket instructions, defined
here minimally and consistently with the spec):
- `ModelPolicy.review_second: str` — the parsed `models.review_second:` value
  (empty string when unset), read back as a plain `provider/model` string.
"""
from pathlib import Path

import pytest

from dispatcher.config import load_config
from dispatcher.models import EFFORTS, Entry, parse_entry, parse_policy

TRACKS = {
    "standard": {
        "when": "Bounded change with a clear scope.",
        "spec": ["claude-opus-5@medium"],
        "plan": ["claude-opus-5@medium"],
        "implement": ["claude-opus-5@medium"],
        "review": ["claude-opus-5@medium"],
    },
}


def _raw(**extra):
    return {"triage": ["anthropic/claude-sonnet-5@medium"], "untracked": "standard",
            "tracks": TRACKS, **extra}


# -- criterion 1: openai efforts are openai's own vocabulary ---------------

def test_openai_max_is_rejected_naming_openais_efforts():
    with pytest.raises(ValueError, match="openai"):
        parse_entry("openai/gpt-sol@max", "t:")


def test_openai_minimal_is_accepted():
    e = parse_entry("openai/gpt-sol@minimal", "t:")
    assert e == Entry("openai", "gpt-sol", "minimal")


@pytest.mark.parametrize("effort", EFFORTS)  # low medium high xhigh max
def test_every_anthropic_effort_behaves_as_before(effort):
    e = parse_entry(f"anthropic/claude-opus-5@{effort}", "t:")
    assert e.effort == effort


# -- criterion 2: provider with no effort vocabulary is rejected -----------

def test_entry_for_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        parse_entry("mistral/some-model@medium", "t:")


# -- criterion 3: triage stays anthropic-only -------------------------------

def test_triage_containing_openai_entry_is_rejected():
    with pytest.raises(ValueError):
        parse_policy(_raw(triage=["anthropic/claude-sonnet-5@medium",
                                  "openai/gpt-sol@medium"]))


# -- criterion 4: models.review_second is optional and validated -----------

def test_review_second_unset_is_empty():
    policy = parse_policy(_raw())
    assert policy.review_second == ""


def test_review_second_valid_openai_model_is_accepted():
    policy = parse_policy(_raw(review_second="openai/gpt-sol"))
    assert policy.review_second == "openai/gpt-sol"


def test_review_second_anthropic_provider_is_rejected():
    with pytest.raises(ValueError):
        parse_policy(_raw(review_second="anthropic/claude-opus-5"))


def test_review_second_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        parse_policy(_raw(review_second="mistral/some-model"))


# -- criterion 5: targets.example.yaml still loads --------------------------

def test_targets_example_yaml_still_loads():
    cfg = load_config(Path(__file__).resolve().parent.parent / "targets.example.yaml")
    assert cfg.models is not None
