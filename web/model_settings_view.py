"""The console's model settings page on the wire: the `models:` block with
structured entries, the save body, and the view. The block's rules live in
dispatcher.models; dispatcher.config applies it."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from dispatcher import model_settings
from dispatcher.config import Config
from dispatcher.models import (DEFAULT_PROVIDER, PROVIDER_EFFORTS, STAGES, Entry,
                               ModelPolicy, entry_text, parse_entry)


class EntrySetting(BaseModel):
    provider: str
    model: str
    effort: str   # "" = the CLI's default

    @classmethod
    def of(cls, entry: Entry) -> EntrySetting:
        return cls(provider=entry.provider, model=entry.model, effort=entry.effort)

    def text(self, context: str) -> str:
        return entry_text(self.provider, self.model, self.effort, context)


class TrackSetting(BaseModel):
    when: str
    plan_review: bool
    spec: list[EntrySetting]
    plan: list[EntrySetting]
    implement: list[EntrySetting]
    review: list[EntrySetting]


class ModelsSetting(BaseModel):
    """The `models:` block with structured entries. models.parse_block
    validates it; this only fixes the shape."""
    triage: list[EntrySetting]
    untracked: str
    tracks: dict[str, TrackSetting]
    pinned: list[str]
    review_second: EntrySetting | None

    @classmethod
    def of(cls, policy: ModelPolicy) -> ModelsSetting:
        return cls(
            triage=_settings(policy.triage), untracked=policy.untracked,
            tracks={t.name: TrackSetting(when=t.when, plan_review=t.plan_review,
                                         **{s: _settings(t.stages[s]) for s in STAGES})
                    for t in policy.tracks.values()},
            pinned=list(policy.pinned),
            review_second=(EntrySetting.of(parse_entry(policy.review_second, "review_second:"))
                           if policy.review_second else None))

    def to_raw(self) -> dict:
        """The targets.yaml shape. Raises ValueError (models.entry_text)."""
        raw: dict = {
            "triage": _texts(self.triage, "triage:"), "untracked": self.untracked,
            "tracks": {name: {"when": t.when, "plan_review": t.plan_review,
                              **{s: _texts(getattr(t, s), f"track {name!r} {s}:")
                                 for s in STAGES}}
                       for name, t in self.tracks.items()},
            "pinned": self.pinned}
        if self.review_second is not None:
            raw["review_second"] = self.review_second.text("review_second:")
        return raw


def _settings(entries: tuple[Entry, ...]) -> list[EntrySetting]:
    return [EntrySetting.of(e) for e in entries]


def _texts(entries: list[EntrySetting], context: str) -> list[str]:
    return [e.text(f"{context}[{i}]") for i, e in enumerate(entries)]


class ModelsSave(BaseModel):
    """A save: the block, and the revision of the view it was edited from."""
    revision: str
    models: ModelsSetting


class ModelSettingsView(BaseModel):
    models: ModelsSetting
    # Send back with a save or reset: a stale one is refused (409).
    revision: str
    # Where `models` comes from: the block stored from this page, or the
    # targets.yaml one (no stored block, or a stored block that failed).
    source: Literal["console", "targets.yaml"]
    set_by: str
    set_at: str
    error: str                      # why a stored block is not in force, "" = none
    efforts: dict[str, list[str]]   # provider -> its effort vocabulary
    default_provider: str           # the provider of a bare model id
    known_models: dict[str, list[str]]  # provider -> models either block names
    target_overrides: list[str]     # targets whose own models: block wins


def view(cfg: Config, revision: str) -> ModelSettingsView:
    origin = cfg.models_origin
    stored = origin if isinstance(origin, model_settings.Stored) else None
    return ModelSettingsView(
        models=ModelsSetting.of(cfg.models), revision=revision,
        source="console" if stored else "targets.yaml",
        set_by=stored.set_by if stored else "", set_at=stored.set_at if stored else "",
        error=origin.reason if isinstance(origin, model_settings.Rejected) else "",
        efforts={p: list(e) for p, e in PROVIDER_EFFORTS.items()},
        default_provider=DEFAULT_PROVIDER,
        known_models=_known_models(cfg),
        target_overrides=[t.name for t in cfg.targets if t.models is not None])


def _known_models(cfg: Config) -> dict[str, list[str]]:
    """provider -> the models the block in force or targets.yaml's names."""
    known: dict[str, set[str]] = {}
    for e in (*cfg.models.entries(), *cfg.file_models.entries()):
        known.setdefault(e.provider, set()).add(e.model)
    return {p: sorted(m) for p, m in sorted(known.items())}
