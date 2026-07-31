"""Triage request/cursor state and repo enumeration."""
import json
import subprocess as _subprocess
from dataclasses import replace
from unittest.mock import patch

import pytest

from dispatcher import triage
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.triage_apply import ApplyResult


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


class SpyNotifier:
    def __init__(self):
        self.sent = []

    def send(self, template, **ctx):
        self.sent.append((template, ctx))
        return 0


class FakeDeps:
    def __init__(self):
        self.notifier = SpyNotifier()


OK_USAGE = UsageSnapshot(utilization=0.1, minutes_to_reset=100, source="oauth")
HOT_USAGE = UsageSnapshot(utilization=0.9, minutes_to_reset=100, source="oauth")
BLOB = {"repo": "o/a", "cursor": "c", "issues": [{"number": 1}],
        "labels": [{"name": "bug", "description": ""}],
        "issue_types": [], "open_issues": []}
RESULT = ApplyResult(labeled=1, comments=0, closes=("close #1 as not planned — spam",),
                     rejected=())


def _sweep_cfg(tmp_path):
    return _cfg(tmp_path, targets=[_target("a", "o/a")], infra="")


def test_sweep_happy_path_advances_cursor_and_reports(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": "2026-07-29T00:00:00+00:00"})
    (tmp_path / triage.REQUEST_FILE).write_text('{"requested_at": "x"}')
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", return_value={"issues": []}), \
         patch.object(triage.triage_apply, "apply", return_value=RESULT):
        triage.run_sweep(cfg, deps)
    assert triage.load_request(tmp_path) is None
    assert triage.load_cursors(tmp_path)["o/a"] != "2026-07-29T00:00:00+00:00"
    [(template, ctx)] = deps.notifier.sent
    assert template == "triage_report"
    joined = "\n".join(ctx["lines"])
    assert "o/a" in joined and "close #1" in joined


def test_sweep_seeds_cursor_first_sight_no_session(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage, "_run_session") as session:
        triage.run_sweep(cfg, deps)
    session.assert_not_called()
    assert "o/a" in triage.load_cursors(tmp_path)


def test_sweep_empty_window_advances_cursor_no_session(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": "2026-07-29T00:00:00+00:00"})
    empty = dict(BLOB, issues=[])
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=empty), \
         patch.object(triage, "_run_session") as session:
        triage.run_sweep(cfg, deps)
    session.assert_not_called()
    assert triage.load_cursors(tmp_path)["o/a"] != "2026-07-29T00:00:00+00:00"


def test_sweep_failure_isolated_cursor_untouched(tmp_path):
    cfg = _cfg(tmp_path, targets=[_target("a", "o/a"), _target("b", "o/b")])
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": "OLD", "o/b": "OLD"})

    def prefetch(repo, cursor, run=None):
        if repo == "o/a":
            raise triage.triage_prefetch.PrefetchError("boom")
        return dict(BLOB, repo="o/b")

    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", side_effect=prefetch), \
         patch.object(triage, "_run_session", return_value={"issues": []}), \
         patch.object(triage.triage_apply, "apply", return_value=RESULT):
        triage.run_sweep(cfg, deps)
    cursors = triage.load_cursors(tmp_path)
    assert cursors["o/a"] == "OLD" and cursors["o/b"] != "OLD"
    joined = "\n".join(deps.notifier.sent[0][1]["lines"])
    assert "o/a: FAILED" in joined


def test_sweep_budget_gate_skips_everything(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": "OLD"})
    with patch.object(triage, "fetch_usage", return_value=HOT_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch") as prefetch:
        triage.run_sweep(cfg, deps)
    prefetch.assert_not_called()
    assert triage.load_cursors(tmp_path)["o/a"] == "OLD"
    assert "budget" in "\n".join(deps.notifier.sent[0][1]["lines"])


def test_sweep_rejected_decisions_reported(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": "OLD"})
    rej = ApplyResult(labeled=0, comments=0, closes=(),
                      rejected=("#1: unknown label(s) ['zzz']",))
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", return_value={"issues": []}), \
         patch.object(triage.triage_apply, "apply", return_value=rej):
        triage.run_sweep(cfg, deps)
    assert "rejected" in "\n".join(deps.notifier.sent[0][1]["lines"])


def test_clone_for(tmp_path):
    cfg = _cfg(tmp_path, targets=[_target("a", "o/a")], infra="o/infra")
    assert triage._clone_for(cfg, "o/a") == "/repos/a"
    assert triage._clone_for(cfg, "o/infra").endswith("/agent-ops")


def test_run_session_timeout_kills_and_raises(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    calls = []

    def fake_run(args, capture_output=True, text=True, timeout=None):
        calls.append(args)
        if args[0] == "podman" and args[1] == "run":
            raise _subprocess.TimeoutExpired(args, timeout)
        return _subprocess.CompletedProcess(args, 0, "", "")

    with pytest.raises(triage.SweepError):
        triage._run_session(cfg, "o/a", BLOB, "2026-07-30", run=fake_run)
    assert ["podman", "kill", "triage-o-a"] in calls


def test_run_session_missing_decisions_raises(tmp_path):
    cfg = _sweep_cfg(tmp_path)

    def fake_run(args, capture_output=True, text=True, timeout=None):
        return _subprocess.CompletedProcess(args, 0, "", "")

    with pytest.raises(triage.SweepError):
        triage._run_session(cfg, "o/a", BLOB, "2026-07-30", run=fake_run)


def test_run_session_reads_decisions(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    tdir = tmp_path / "triage"
    tdir.mkdir()
    captured = []

    def fake_run(args, capture_output=True, text=True, timeout=None):
        captured.append(args)
        (tdir / "o-a-2026-07-30.json").write_text('{"issues": []}')
        return _subprocess.CompletedProcess(args, 0, "", "")

    got = triage._run_session(cfg, "o/a", BLOB, "2026-07-30", run=fake_run)
    assert got == {"issues": []}
    argv = captured[0]
    assert f"{tdir}:/triage" in argv
    prompt = argv[argv.index("-p") + 1]
    assert "/triage/o-a-2026-07-30.json" in prompt


def test_run_session_nonzero_rc_surfaces_rc_and_stderr(tmp_path):
    cfg = _sweep_cfg(tmp_path)

    def fake_run(args, capture_output=True, text=True, timeout=None):
        return _subprocess.CompletedProcess(args, 1, "", "fatal: image pull failed")

    with pytest.raises(triage.SweepError) as exc_info:
        triage._run_session(cfg, "o/a", BLOB, "2026-07-30", run=fake_run)
    msg = str(exc_info.value)
    assert "rc=1" in msg
    assert "fatal: image pull failed" in msg


def test_guarded_sweep_notifies_and_reraises(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    with patch.object(triage, "run_sweep", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            triage.guarded_sweep(cfg, deps)
    assert "sweep crashed" in "\n".join(deps.notifier.sent[0][1]["lines"])
