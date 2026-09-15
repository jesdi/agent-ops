"""Which model, at which effort, runs which stage of which task.

Pure policy: `parse_policy` turns the `models:` block of targets.yaml into
frozen dataclasses (raising at config-load time on anything malformed), and
`resolve` walks a track's stage list to the first entry the usage gate
admits. No I/O, and no import of dispatcher.state — callers pass `stage` as
a plain string. See docs/specs/2026-09-14-model-tracks-design.md."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

STAGES = ("spec", "plan", "implement", "review")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-opus-5"
TRACK_LABEL_PREFIX = "track:"
_POLICY_STAGES = {"queued": "spec", "awaiting-spec-review": "spec",
                  "address-review": "implement"}
_OLD_KEYS = ("default", "rules")
_TOP_KEYS = frozenset({"triage", "untracked", "tracks"})


def split_model_id(model_id: str) -> tuple[str, str]:
    """`provider/model` -> (provider, model); a bare id is anthropic's. The
    same bare model under two providers spends two different usage windows,
    so the provider is part of the id, never inferred from the model name."""
    if "/" not in model_id:
        return DEFAULT_PROVIDER, model_id
    provider, _, bare = model_id.partition("/")
    if not provider or not bare or "/" in bare:
        raise ValueError(f"model id must be 'provider/model' or bare, got {model_id!r}")
    return provider, bare


def bare_model_id(model_id: str) -> str:
    return split_model_id(model_id)[1]


def policy_stage(stage: str) -> str:
    """Runtime stage -> the four-stage policy vocabulary. Queued and the
    approval gate run the spec model; address-review is implementation work.
    Anything else passes through (and, if it is not a policy stage, resolves
    to nothing)."""
    return _POLICY_STAGES.get(stage, stage)


@dataclass(frozen=True)
class Entry:
    """One element of a track's stage list: provider, bare model, effort
    ("" = the CLI's default). Written `provider/model[@effort]`."""
    provider: str
    model: str
    effort: str = ""

    @property
    def model_id(self) -> str:
        return f"{self.provider}/{self.model}"

    def __str__(self) -> str:
        return self.model_id + (f"@{self.effort}" if self.effort else "")


def parse_entry(value: object, context: str) -> Entry:
    """`provider/model[@effort]` -> Entry. Whitespace is rejected because the
    value lands unquoted in a shell command; a bad effort is rejected because
    the CLI would."""
    if not isinstance(value, str) or value == "" or any(c.isspace() for c in value):
        raise ValueError(f"models: {context} must be a non-empty entry "
                         f"'provider/model[@effort]' with no whitespace, got {value!r}")
    model_id, at, effort = value.partition("@")
    if at and effort not in EFFORTS:
        raise ValueError(f"models: {context} effort must be one of "
                         f"{list(EFFORTS)}, got {effort!r}")
    try:
        provider, model = split_model_id(model_id)
    except ValueError as e:
        raise ValueError(f"models: {context} {e}") from e
    return Entry(provider, model, effort)


def _entries(raw: object, context: str) -> tuple[Entry, ...]:
    if isinstance(raw, str) or not isinstance(raw, list) or not raw:
        raise ValueError(f"models: {context} must be a non-empty list of "
                         f"entries, got {raw!r}")
    return tuple(parse_entry(v, f"{context}[{i}]") for i, v in enumerate(raw))


@dataclass(frozen=True)
class Track:
    name: str
    when: str
    stages: Mapping[str, tuple[Entry, ...]]  # every STAGES key present


@dataclass(frozen=True)
class ModelPolicy:
    triage: tuple[Entry, ...]
    untracked: str          # the track a candidate with no track: label specs on
    tracks: Mapping[str, Track]

    def entries(self) -> list[Entry]:
        out = list(self.triage)
        for track in self.tracks.values():
            for stage in STAGES:
                out.extend(track.stages[stage])
        return out

    def model_ids(self) -> list[str]:
        """Every model id this policy can launch, deduplicated, order kept."""
        return list(dict.fromkeys(e.model_id for e in self.entries()))

    def gate_entry(self) -> Entry:
        """What an idle box would spawn next: the untracked track's first
        spec entry. The console header and the stall/resume pings key on it."""
        return self.tracks[self.untracked].stages["spec"][0]


_STANDARD = Track("standard", "Any task.",
                  {s: (Entry(DEFAULT_PROVIDER, DEFAULT_MODEL),) for s in STAGES})
DEFAULT_POLICY = ModelPolicy(triage=_STANDARD.stages["spec"], untracked="standard",
                             tracks={"standard": _STANDARD})


def _track(name: str, raw: object) -> Track:
    if not name or ":" in name or any(c.isspace() for c in name):
        raise ValueError(f"models: track name {name!r} must be non-empty with no "
                         f"whitespace or ':' (it becomes the label track:<name>)")
    if not isinstance(raw, dict):
        raise ValueError(f"models: track {name!r} must be a mapping, got {raw!r}")
    unknown = set(raw) - set(STAGES) - {"when"}
    if unknown:
        raise ValueError(f"models: track {name!r} has unknown key(s) "
                         f"{sorted(unknown)}; expected when: plus {list(STAGES)}")
    missing = [s for s in STAGES if s not in raw]
    if missing:
        raise ValueError(f"models: track {name!r} names no {missing} list; "
                         f"every track names all of {list(STAGES)}")
    when = raw.get("when")
    if not isinstance(when, str) or not when.strip():
        raise ValueError(f"models: track {name!r} needs a non-empty when: sentence")
    return Track(name, when.strip(),
                 {s: _entries(raw[s], f"track {name!r} {s}:") for s in STAGES})


def parse_policy(raw: dict | None) -> ModelPolicy:
    """Validate the `models:` block. Raises ValueError so a typo kills the
    pass loudly at config load instead of silently mis-routing."""
    if not raw:
        return DEFAULT_POLICY
    if not isinstance(raw, dict):
        raise ValueError(f"models: must be a mapping, got {raw!r}")
    old = [k for k in _OLD_KEYS if k in raw]
    if old:
        raise ValueError(f"models: {old} are gone; write tracks:, untracked: "
                         f"and triage: (see targets.example.yaml)")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ValueError(f"models: unknown key(s) {sorted(unknown)}; expected "
                         f"tracks:, untracked:, triage:")
    tracks_raw = raw.get("tracks")
    if not isinstance(tracks_raw, dict) or not tracks_raw:
        raise ValueError("models: tracks: must be a non-empty mapping of "
                         "track name to track")
    tracks = {str(n): _track(str(n), t) for n, t in tracks_raw.items()}
    untracked = raw.get("untracked")
    if untracked not in tracks:
        raise ValueError(f"models: untracked: must name a defined track "
                         f"{sorted(tracks)}, got {untracked!r}")
    if "triage" not in raw:
        raise ValueError("models: triage: list is required")
    return ModelPolicy(triage=_entries(raw["triage"], "triage:"),
                       untracked=untracked, tracks=tracks)


def tracks_text(policy: ModelPolicy) -> str:
    """The track list as the prompts show it: one line per track."""
    return "\n".join(f"- `{t.name}`: {t.when}" for t in policy.tracks.values())


def track_from_labels(labels: Sequence[str], policy: ModelPolicy) -> str:
    """The first `track:<name>` label naming a configured track, else the
    untracked track. Only ever decides who writes the spec."""
    for label in labels:
        if label.startswith(TRACK_LABEL_PREFIX):
            name = label[len(TRACK_LABEL_PREFIX):]
            if name in policy.tracks:
                return name
    return policy.untracked
