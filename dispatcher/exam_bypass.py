"""The owner's bypass labels: what a requested exam does about `skip` and `defer`.

The config is `.pr-exam.json` read at the PR's base ref, never its head. A label counts
only when its most recent `labeled` event was made by `operator_login`."""
from __future__ import annotations

import tempfile
from pathlib import Path

CONFIG = ".pr-exam.json"


def _config(io, repo: str, base_ref: str) -> dict:
    # ponytail: fakes of earlier tickets' locked tests lack `config`; drop the hasattr once they gain it
    text = io.github.file(repo, base_ref, CONFIG) if hasattr(io.pr_exam, "config") else None
    if text is None:
        return {"errors": [], "enabled": True, "labels": {}}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / CONFIG
        path.write_text(text)
        try:
            return io.pr_exam.config(path)
        except ValueError as exc:   # the skill refused the config: its output is the errors
            return {"errors": [str(exc)], "enabled": True, "labels": {}}


def _applied_by(events: list[dict], label: str) -> str | None:
    """Who last added `label`; None when it was never added."""
    actors = [e["actor"] for e in events if e["event"] == "labeled" and e["label"] == label]
    return actors[-1] if actors else None


def decide(cfg, repo: str, n: int, pr: dict, io) -> tuple[str, str]:
    """(mode, detail) for the PR as `pr` read it: mode is "error", "disabled", "skip",
    "defer" or "none"."""
    conf = _config(io, repo, pr["base_ref"])
    if conf["errors"]:
        return "error", "; ".join(conf["errors"])
    if not conf["enabled"]:
        return "disabled", ""
    marked = [label for label in pr.get("labels", []) if label in conf["labels"]]
    if not marked:
        return "none", ""
    if not cfg.operator_login:
        return "error", "operator_login is not set in targets.yaml, so no bypass label can be honoured"
    events = io.github.label_events(repo, n)
    modes = {conf["labels"][label] for label in marked
             if _applied_by(events, label) == cfg.operator_login}
    # ponytail: skip outranks defer when the owner added both labels
    return next((m for m in ("skip", "defer") if m in modes), "none"), ""
