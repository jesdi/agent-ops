"""Which Claude model runs which stage of which task.

Pure policy: `parse_policy` turns the `models:` block of targets.yaml into
frozen dataclasses (raising at config-load time on anything malformed), and
`resolve` maps (policy, stage, effort, labels) to a model string. No I/O, and
no import of dispatcher.state — callers pass `stage` as a plain string."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

DEFAULT_MODEL = "claude-opus-4-8"
STAGES = ("spec", "plan", "implement")

_WHEN_KEYS = frozenset({"effort", "labels_include", "labels_exclude"})
_EFFORT_KEYS = frozenset({"min", "max"})


@dataclass(frozen=True)
class ModelRule:
    name: str
    effort_min: int | None
    effort_max: int | None
    labels_include: tuple[str, ...]
    labels_exclude: tuple[str, ...]
    use: str | dict[str, str]


@dataclass(frozen=True)
class ModelPolicy:
    default: str
    rules: tuple[ModelRule, ...]


DEFAULT_POLICY = ModelPolicy(default=DEFAULT_MODEL, rules=())


def _labels(when: dict, key: str) -> tuple[str, ...]:
    raw = when.get(key, ())
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise ValueError(f"models: {key} must be a list of label names, got {raw!r}")
    return tuple(str(x) for x in raw)


def _effort_bounds(when: dict) -> tuple[int | None, int | None]:
    raw = when.get("effort")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError(f"models: effort must be a mapping of min/max, got {raw!r}")
    unknown = set(raw) - _EFFORT_KEYS
    if unknown:
        raise ValueError(
            f"models: effort accepts exactly min/max, got unknown {sorted(unknown)}")
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"models: effort {key} must be an integer, got {value!r}")
    return raw.get("min"), raw.get("max")


def _check_model_id(value: object, context: str) -> str:
    """A model id must be a non-empty string with no whitespace: no allowlist
    of known ids (deliberately out of scope), but an unusable value (empty,
    a dict coerced via str(), or something with embedded whitespace that
    would land unquoted in a shell command) must kill the pass at config
    load rather than mis-resolve silently at spawn time."""
    if not isinstance(value, str) or value == "" or any(c.isspace() for c in value):
        raise ValueError(
            f"models: {context} must be a non-empty model id with no "
            f"whitespace, got {value!r}")
    return value


def _use(rule: dict, name: str) -> str | dict[str, str]:
    if "use" not in rule:
        raise ValueError(f"models: rule {name!r} has no use:")
    raw = rule["use"]
    if isinstance(raw, str):
        return _check_model_id(raw, f"rule {name!r} use:")
    if not isinstance(raw, dict):
        raise ValueError(f"models: rule {name!r} use: must be a model or a stage map")
    unknown = set(raw) - set(STAGES)
    if unknown:
        raise ValueError(
            f"models: rule {name!r} use: has unknown stage(s) {sorted(unknown)}; "
            f"expected any of {list(STAGES)}")
    return {k: _check_model_id(v, f"rule {name!r} use.{k}:") for k, v in raw.items()}


def _rule(raw: dict, index: int) -> ModelRule:
    if not isinstance(raw, dict):
        raise ValueError(f"models: rule #{index} must be a mapping, got {raw!r}")
    name = str(raw.get("name", f"rule-{index}"))
    when = raw.get("when") or {}
    if not isinstance(when, dict):
        raise ValueError(f"models: rule {name!r} when: must be a mapping")
    unknown = set(when) - _WHEN_KEYS
    if unknown:
        raise ValueError(
            f"models: rule {name!r} when: has unknown key(s) {sorted(unknown)}; "
            f"expected any of {sorted(_WHEN_KEYS)}")
    effort_min, effort_max = _effort_bounds(when)
    return ModelRule(
        name=name,
        effort_min=effort_min,
        effort_max=effort_max,
        labels_include=_labels(when, "labels_include"),
        labels_exclude=_labels(when, "labels_exclude"),
        use=_use(raw, name),
    )


def parse_policy(raw: dict | None) -> ModelPolicy:
    """Validate the `models:` block. Raises ValueError so a typo kills the
    pass loudly at config load instead of silently never matching."""
    if not raw:
        return DEFAULT_POLICY
    if not isinstance(raw, dict):
        raise ValueError(f"models: must be a mapping, got {raw!r}")
    default = _check_model_id(raw.get("default", DEFAULT_MODEL), "default")
    rules = raw.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError(f"models: rules must be a list, got {rules!r}")
    return ModelPolicy(default=default,
                       rules=tuple(_rule(r, i) for i, r in enumerate(rules)))


def _matches(rule: ModelRule, effort: int | None, labels: frozenset[str]) -> bool:
    # An unset effort never satisfies an effort constraint — fail closed to the
    # default rather than downgrading an unscored task.
    if rule.effort_min is not None and (effort is None or effort < rule.effort_min):
        return False
    if rule.effort_max is not None and (effort is None or effort > rule.effort_max):
        return False
    if any(l not in labels for l in rule.labels_include):
        return False
    if any(l in labels for l in rule.labels_exclude):
        return False
    return True


def resolve(policy: ModelPolicy, stage: str, effort: int | None,
            labels: Sequence[str]) -> str:
    """First matching rule wins; a stage the winning rule doesn't name falls
    back to the policy default, not to later rules."""
    have = frozenset(labels)
    for rule in policy.rules:
        if not _matches(rule, effort, have):
            continue
        if isinstance(rule.use, str):
            return rule.use
        return rule.use.get(stage, policy.default)
    return policy.default
