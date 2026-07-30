"""Triage request/cursor state and repo enumeration."""
import json
from dataclasses import replace

import pytest

from dispatcher import triage
from dispatcher.config import Config, Target


def _cfg(tmp_path, targets=(), infra=""):
    return Config(
        state_dir=str(tmp_path), capacity=2, budget_threshold=0.8,
        racing_minutes=30, racing_threshold=0.95, session_memory="1500m",
        session_cpus="2", targets=list(targets), infra_repo=infra)


def _target(name, repo):
    return Target(
        name=name, repo=repo, clone_path=f"/repos/{name}",
        worktrees_path=f"/repos/{name}.wt", rank_cmd="", setup_cmd="",
        verify_cmd="", project_number=1, project_owner="o",
        status_field_id="f", status_ready_option_id="r",
        status_in_progress_option_id="p")


def test_enqueue_writes_request(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "running", lambda: False)
    assert triage.enqueue(tmp_path) is True
    assert triage.load_request(tmp_path) is not None


def test_enqueue_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "running", lambda: False)
    triage.enqueue(tmp_path)
    first = triage.load_request(tmp_path)
    assert triage.enqueue(tmp_path) is False
    assert triage.load_request(tmp_path) == first


def test_enqueue_noop_while_running(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "running", lambda: True)
    assert triage.enqueue(tmp_path) is False
    assert triage.load_request(tmp_path) is None


def test_clear_request_idempotent(tmp_path):
    triage.clear_request(tmp_path)  # nothing to clear: no raise
    assert triage.load_request(tmp_path) is None


def test_expired():
    assert triage.expired(
        "2026-07-30T05:30:00+00:00", "2026-07-30T07:30:01+00:00") is True
    assert triage.expired(
        "2026-07-30T05:30:00+00:00", "2026-07-30T07:29:59+00:00") is False


def test_cursors_roundtrip(tmp_path):
    assert triage.load_cursors(tmp_path) == {}
    triage.save_cursors(tmp_path, {"o/r": "2026-07-30T05:30:00+00:00"})
    assert triage.load_cursors(tmp_path) == {"o/r": "2026-07-30T05:30:00+00:00"}


def test_triage_repos_targets_then_infra_deduped(tmp_path):
    cfg = _cfg(tmp_path,
               targets=[_target("a", "o/a"), _target("b", "o/b")],
               infra="o/infra")
    assert triage.triage_repos(cfg) == ["o/a", "o/b", "o/infra"]
    cfg2 = replace(cfg, infra_repo="o/a")
    assert triage.triage_repos(cfg2) == ["o/a", "o/b"]
    cfg3 = replace(cfg, infra_repo="")
    assert triage.triage_repos(cfg3) == ["o/a", "o/b"]


def test_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "running", lambda: False)
    assert triage.pending(tmp_path) is False
    triage.enqueue(tmp_path)
    assert triage.pending(tmp_path) is True
    triage.clear_request(tmp_path)
    monkeypatch.setattr(triage, "running", lambda: True)
    assert triage.pending(tmp_path) is True
