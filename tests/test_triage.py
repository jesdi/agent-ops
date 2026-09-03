"""Triage request/cursor state and repo enumeration."""
import json
import re
import subprocess as _subprocess
import sys
from dataclasses import replace
from unittest.mock import patch

import pytest

from dispatcher import herdr, triage
from dispatcher.budget import UsageSnapshot
from dispatcher.config import Config, Target
from dispatcher.triage_apply import ApplyResult

H_NO_TABS = '{"id":"i","result":{"tabs":[],"type":"tab_list"}}'
H_TRIAGE_TAB = ('{"id":"i","result":{"tabs":[{"label":"triage","tab_id":"w9:t2",'
                '"workspace_id":"w9","number":2,"focused":false,"pane_count":1,'
                '"agent_status":"unknown"}],"type":"tab_list"}}')
H_SYS_WS = ('{"id":"i","result":{"type":"workspace_list","workspaces":[{"label":"agent-ops",'
            '"workspace_id":"w9","number":9,"focused":false,"pane_count":1,'
            '"tab_count":1,"active_tab_id":"w9:t1","agent_status":"unknown"}]}}')
H_NO_WS = '{"id":"i","result":{"type":"workspace_list","workspaces":[]}}'
H_TAB_CREATED = ('{"id":"i","result":{"tab":{"label":"triage","tab_id":"w9:t2",'
                 '"workspace_id":"w9","number":2,"focused":false,"pane_count":1,'
                 '"agent_status":"unknown"},"root_pane":{"pane_id":"w9:p2",'
                 '"tab_id":"w9:t2","workspace_id":"w9","agent_status":"unknown",'
                 '"focused":false,"revision":0,"terminal_id":"t"},"type":"tab_created"}}')
H_WS_CREATED = ('{"id":"i","result":{"workspace":{"workspace_id":"w9","label":"agent-ops",'
                '"number":9,"focused":false,"pane_count":1,"tab_count":1,'
                '"active_tab_id":"w9:t1","agent_status":"unknown"},'
                '"tab":{"label":"1","tab_id":"w9:t1","workspace_id":"w9",'
                '"number":1,"focused":false,"pane_count":1,"agent_status":"unknown"},'
                '"root_pane":{"pane_id":"w9:p1","tab_id":"w9:t1","workspace_id":"w9",'
                '"agent_status":"unknown","focused":false,"revision":0,"terminal_id":"t"},'
                '"type":"workspace_created"}}')


def _herdr_fake(monkeypatch, table, calls=None):
    def _run(args):
        if calls is not None:
            calls.append(args)
        for prefix, rc, out in table:
            if args[:len(prefix)] == list(prefix):
                return _subprocess.CompletedProcess(["herdr", *args], rc, out, "")
        return None
    monkeypatch.setattr(herdr, "_run", _run)


H_SYS_PANES = ('{"id":"i","result":{"panes":[{"pane_id":"w9:p1","tab_id":"w9:t1",'
               '"workspace_id":"w9","agent_status":"unknown","focused":false,'
               '"revision":0,"terminal_id":"a"},{"pane_id":"w9:p2","tab_id":"w9:t2",'
               '"workspace_id":"w9","agent_status":"unknown","focused":false,'
               '"revision":3,"terminal_id":"b"}],"type":"pane_list"}}')


def _busy(shell_pid, pgid):
    return (("pane", "process-info"), 0,
            '{"id":"i","result":{"process_info":{"foreground_process_group_id":%d,'
            '"foreground_processes":[],"pane_id":"w9:p2","shell_pid":%d},'
            '"type":"pane_process_info"}}' % (pgid, shell_pid))


def _herdr_fake_creating(monkeypatch, calls, workspace=None, pane_run_ok=True):
    """Stateful fake for Tab.ensure: first `tab list` returns H_NO_TABS;
    after `tab create` flips the flag, subsequent `tab list` calls return
    H_TRIAGE_TAB (the re-resolve step Tab.ensure performs after creating).
    Any `pane list` returns H_SYS_PANES. Pass workspace=H_NO_WS to
    exercise the workspace-create path; default (None) uses H_SYS_WS."""
    ws_resp = H_SYS_WS if workspace is None else workspace
    made = {"tab": False}

    def _run(args):
        calls.append(args)

        def ok(out):
            return _subprocess.CompletedProcess(["herdr", *args], 0, out, "")

        head = args[:2]
        if head == ["tab", "list"]:
            return ok(H_TRIAGE_TAB if made["tab"] else H_NO_TABS)
        if head == ["pane", "list"]:
            return ok(H_SYS_PANES)
        if head == ["workspace", "list"]:
            return ok(ws_resp)
        if head == ["workspace", "create"]:
            return ok(H_WS_CREATED)
        if head == ["tab", "create"]:
            made["tab"] = True
            return ok(H_TAB_CREATED)
        if head == ["pane", "run"]:
            if not pane_run_ok:
                return _subprocess.CompletedProcess(
                    ["herdr", *args], 1,
                    '{"error":{"code":"pane_not_found","message":"x"},"id":"i"}', "")
            return ok("")
        return None

    monkeypatch.setattr(herdr, "_run", _run)




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


def test_expired_fails_closed_on_unparseable_timestamp():
    # A corrupt request file must expire (tick then clears it), not raise out
    # of every dispatcher pass.
    assert triage.expired("x", "2026-07-30T07:30:01+00:00") is True
    assert triage.expired("", "2026-07-30T07:30:01+00:00") is True


def test_tick_clears_corrupt_request_instead_of_raising(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: False)
    (tmp_path / triage.REQUEST_FILE).write_text('{"requested_at": "x"}')
    calls = []
    monkeypatch.setattr(triage.subprocess, "run",
                        lambda args, **k: calls.append(args))
    triage.tick(cfg, deps, "targets.yaml")  # no raise
    assert triage.load_request(tmp_path) is None
    assert calls == []
    assert deps.notifier.sent[0][1]["lines"] == [
        "skipped — no capacity within 2 h"]


def test_cursors_roundtrip(tmp_path):
    assert triage.load_cursors(tmp_path) == {}
    triage.save_cursors(tmp_path, {"o/r": "2026-07-30T05:30:00Z"})
    assert triage.load_cursors(tmp_path) == {"o/r": "2026-07-30T05:30:00Z"}


def test_cursor_now_is_second_granular_z_form():
    # GitHub's search date grammar has no fractional seconds; a cursor that
    # carries them degrades the qualifier to free text and matches nothing.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        triage._cursor_now())


def test_load_cursors_rewrites_stale_microsecond_cursors(tmp_path):
    triage.save_cursors(tmp_path, {"o/r": "2026-07-30T05:30:00.123456+00:00",
                                   "o/s": "2026-07-30T07:30:00+02:00"})
    assert triage.load_cursors(tmp_path) == {"o/r": "2026-07-30T05:30:00Z",
                                             "o/s": "2026-07-30T05:30:00Z"}


def test_load_cursors_drops_uninterpretable_cursor(tmp_path):
    triage.save_cursors(tmp_path, {"o/r": "yesterday",
                                   "o/s": "2026-07-30T05:30:00Z"})
    # dropped, not passed through: the repo re-seeds instead of querying with
    # a qualifier GitHub silently ignores forever
    assert triage.load_cursors(tmp_path) == {"o/s": "2026-07-30T05:30:00Z"}


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


OLD = "2026-07-29T00:00:00Z"
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
    triage.save_cursors(tmp_path, {"o/a": OLD})
    (tmp_path / triage.REQUEST_FILE).write_text('{"requested_at": "x"}')
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", return_value={"issues": []}), \
         patch.object(triage.triage_apply, "apply", return_value=RESULT):
        triage.run_sweep(cfg, deps)
    assert triage.load_request(tmp_path) is None
    assert triage.load_cursors(tmp_path)["o/a"] != OLD
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
    triage.save_cursors(tmp_path, {"o/a": OLD})
    empty = dict(BLOB, issues=[])
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=empty), \
         patch.object(triage, "_run_session") as session:
        triage.run_sweep(cfg, deps)
    session.assert_not_called()
    assert triage.load_cursors(tmp_path)["o/a"] != OLD


def test_sweep_failure_isolated_cursor_untouched(tmp_path):
    cfg = _cfg(tmp_path, targets=[_target("a", "o/a"), _target("b", "o/b")])
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD, "o/b": OLD})

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
    assert cursors["o/a"] == OLD and cursors["o/b"] != OLD
    joined = "\n".join(deps.notifier.sent[0][1]["lines"])
    assert "o/a: FAILED" in joined


def test_sweep_budget_gate_skips_everything(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD})
    with patch.object(triage, "fetch_usage", return_value=HOT_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch") as prefetch:
        triage.run_sweep(cfg, deps)
    prefetch.assert_not_called()
    assert triage.load_cursors(tmp_path)["o/a"] == OLD
    assert "budget" in "\n".join(deps.notifier.sent[0][1]["lines"])


def test_sweep_holds_cursor_when_every_write_failed(tmp_path):
    """apply no longer raises on a gh failure, so an expired token or a rate
    limit would otherwise complete the repo and advance its cursor — that
    window would never be re-triaged. Nothing written + gh failures = hold."""
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD})
    failed = ApplyResult(
        labeled=0, comments=0, closes=(),
        rejected=("#1: gh issue edit failed: HTTP 401",),
        write_failures=("#1: gh issue edit failed: HTTP 401",))
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", return_value={"issues": []}), \
         patch.object(triage.triage_apply, "apply", return_value=failed):
        triage.run_sweep(cfg, deps)
    assert triage.load_cursors(tmp_path)["o/a"] == OLD
    joined = "\n".join(deps.notifier.sent[0][1]["lines"])
    assert "cursor held" in joined


def test_sweep_advances_cursor_when_some_writes_landed(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD})
    partial = ApplyResult(
        labeled=1, comments=0, closes=(),
        rejected=("#2: gh issue edit failed: HTTP 500",),
        write_failures=("#2: gh issue edit failed: HTTP 500",))
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", return_value={"issues": []}), \
         patch.object(triage.triage_apply, "apply", return_value=partial):
        triage.run_sweep(cfg, deps)
    # re-running the window would duplicate the write that DID land
    assert triage.load_cursors(tmp_path)["o/a"] != OLD
    assert "cursor held" not in "\n".join(deps.notifier.sent[0][1]["lines"])


def test_sweep_advances_cursor_when_rejections_are_only_validation(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD})
    invalid = ApplyResult(labeled=0, comments=0, closes=(),
                          rejected=("#1: unknown label(s) ['zzz']",))
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", return_value={"issues": []}), \
         patch.object(triage.triage_apply, "apply", return_value=invalid):
        triage.run_sweep(cfg, deps)
    # nothing to retry: the agent's decision was invalid, not GitHub's fault
    assert triage.load_cursors(tmp_path)["o/a"] != OLD


def test_sweep_reports_an_unfittable_context_and_holds_the_cursor(tmp_path):
    """ContextTooLargeError is the diagnosable end of the shed ladder: this
    repo fails visibly with its cursor intact, rather than the container
    getting an argv it cannot exec."""
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD})
    boom = triage.triage_prefetch.ContextTooLargeError(
        "triage context is 200000 bytes with everything shed")
    with patch.object(triage, "fetch_usage", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=BLOB), \
         patch.object(triage, "_run_session", side_effect=boom):
        triage.run_sweep(cfg, deps)
    assert triage.load_cursors(tmp_path)["o/a"] == OLD
    joined = "\n".join(deps.notifier.sent[0][1]["lines"])
    assert "o/a: FAILED" in joined and "everything shed" in joined


def test_sweep_rejected_decisions_reported(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    triage.save_cursors(tmp_path, {"o/a": OLD})
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
        # The spawn argv is wrapper-prefixed (with-claude-token.sh); the
        # kill argv is bare podman — match the run form positionally.
        if "podman" in args and args[args.index("podman") + 1] == "run":
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
    # The prompt travels as a file on the /triage mount, not as an argv string
    prompt = (tdir / "o-a-2026-07-30-prompt.md").read_text()
    assert "/triage/o-a-2026-07-30.json" in prompt
    assert "o/a" in prompt
    assert "/triage/o-a-2026-07-30-prompt.md" in argv[-1]
    assert not any(prompt in a for a in argv)


def test_run_session_prompt_never_in_argv_for_a_huge_blob(tmp_path):
    """MAX_ARG_STRLEN is 128 KiB per argv element; a blob past it used to make
    subprocess.run raise E2BIG, failing the repo every morning forever."""
    cfg = _sweep_cfg(tmp_path)
    tdir = tmp_path / "triage"
    tdir.mkdir()
    huge = dict(BLOB, issues=[{"number": n, "title": "t", "body": "x" * 20000,
                               "comments": []} for n in range(50)])
    captured = []

    def fake_run(args, capture_output=True, text=True, timeout=None):
        captured.append(args)
        (tdir / "o-a-2026-07-30.json").write_text('{"issues": []}')
        return _subprocess.CompletedProcess(args, 0, "", "")

    triage._run_session(cfg, "o/a", huge, "2026-07-30", run=fake_run)
    assert max(len(a) for a in captured[0]) < 4096
    prompt = (tdir / "o-a-2026-07-30-prompt.md").read_text()
    # bounded, and the session is told the context was cut
    assert len(prompt) < 100_000
    assert "truncated" in prompt


def test_run_session_prompt_stays_under_the_argv_ceiling(tmp_path):
    """The end-to-end invariant: the *rendered prompt* — blob plus template —
    is what the container passes as one argv element, so the worst window
    prefetch can produce must land under MAX_ARG_STRLEN with margin."""
    from dispatcher.triage_prefetch import ARGV_CEILING_BYTES
    cfg = _sweep_cfg(tmp_path)
    tdir = tmp_path / "triage"
    tdir.mkdir()
    worst = {
        "repo": "o/a", "cursor": "2026-07-30T05:30:00Z",
        "issues": [{"number": n, "title": "T" * 250, "body": "b" * 5000,
                    "author": "a", "labels": ["inbox"],
                    "comments": [{"author": "x", "body": "c" * 900}
                                 for _ in range(20)]}
                   for n in range(100)],
        "labels": [{"name": f"label-name-{i}", "description": "d" * 120}
                   for i in range(400)],
        "issue_types": [],
        "open_issues": [{"number": n, "title": "t" * 60} for n in range(500)]}

    def fake_run(args, capture_output=True, text=True, timeout=None):
        (tdir / "o-a-2026-07-30.json").write_text('{"issues": []}')
        return _subprocess.CompletedProcess(args, 0, "", "")

    triage._run_session(cfg, "o/a", worst, "2026-07-30", run=fake_run)
    written = (tdir / "o-a-2026-07-30-prompt.md").read_text().encode()
    assert len(written) < ARGV_CEILING_BYTES
    assert len(written) < 0.75 * ARGV_CEILING_BYTES  # real margin, not 5%


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


def test_tick_noop_while_running(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: True)
    launched = []
    monkeypatch.setattr(triage.subprocess, "run",
                        lambda *a, **k: launched.append(a))
    (tmp_path / triage.REQUEST_FILE).write_text(
        '{"requested_at": "2020-01-01T00:00:00+00:00"}')
    triage.tick(cfg, deps, "targets.yaml")
    assert launched == [] and deps.notifier.sent == []


def test_tick_expired_clears_and_notifies(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: False)
    (tmp_path / triage.REQUEST_FILE).write_text(
        '{"requested_at": "2020-01-01T00:00:00+00:00"}')
    triage.tick(cfg, deps, "targets.yaml")
    assert triage.load_request(tmp_path) is None
    [(template, ctx)] = deps.notifier.sent
    assert template == "triage_report"
    assert ctx["lines"] == ["skipped — no capacity within 2 h"]


def _spy_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        triage.subprocess, "run",
        lambda args, **k: calls.append(args) or
        _subprocess.CompletedProcess(args, 0, "", ""))
    return calls


def test_running_is_true_only_for_a_busy_triage_tab(monkeypatch):
    # Busy tab → running.
    _herdr_fake(monkeypatch, [(("tab", "list"), 0, H_TRIAGE_TAB),
                              (("pane", "list"), 0, H_SYS_PANES), _busy(100, 200)])
    assert triage.running() is True
    # Tab present but idle at its prompt (runner finished, or a fresh shell
    # restored after a server restart) is NOT running.
    _herdr_fake(monkeypatch, [(("tab", "list"), 0, H_TRIAGE_TAB),
                              (("pane", "list"), 0, H_SYS_PANES), _busy(100, 100)])
    assert triage.running() is False
    # No tab → not running.
    _herdr_fake(monkeypatch, [(("tab", "list"), 0, H_NO_TABS)])
    assert triage.running() is False
    # Server down → not running.
    _herdr_fake(monkeypatch, [])
    assert triage.running() is False


def test_running_treats_unknown_busyness_as_running(monkeypatch):
    """process-info failing on a tab that exists must not launch a second
    sweep on top of a possibly-live one: fail closed to 'running'."""
    _herdr_fake(monkeypatch, [(("tab", "list"), 0, H_TRIAGE_TAB),
                              (("pane", "list"), 0, H_SYS_PANES)])
    assert triage.running() is True


def test_tick_replaces_a_stale_idle_triage_tab(tmp_path, monkeypatch):
    """After a herdr server restart the `triage` tab is restored as a bare
    shell wearing its old label. Tab.ensure detects it is idle and closes it
    before creating a fresh one, so the label never carries two tabs."""
    cfg = _sweep_cfg(tmp_path)
    monkeypatch.setattr(triage, "running", lambda: False)
    calls = []
    # Stale tab: Tab.find resolves it (tab list + pane list), alive check says
    # idle (shell_pid == pgid), Tab.ensure closes it, then creates fresh one.
    # Tab.ensure re-resolves after create (second tab list + pane list).
    _herdr_fake(monkeypatch, [(("tab", "list"), 0, H_TRIAGE_TAB),
                              (("pane", "list"), 0, H_SYS_PANES),
                              _busy(100, 100),  # idle: pgid == shell_pid
                              (("tab", "close"), 0, '{"id":"i","result":{"type":"ok"}}'),
                              (("workspace", "list"), 0, H_SYS_WS),
                              (("tab", "create"), 0, H_TAB_CREATED),
                              (("pane", "run"), 0, "")], calls)
    triage.enqueue(tmp_path)
    triage.tick(cfg, FakeDeps(), "targets.yaml")
    assert ["tab", "close", "w9:t2"] in calls
    close_i = calls.index(["tab", "close", "w9:t2"])
    create_i = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "create"])
    assert close_i < create_i
    assert sum(1 for c in calls if c[:2] == ["tab", "create"]) == 1


def test_tick_launches_runner_in_a_herdr_tab_when_capacity_free(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: False)
    for var in triage.LAUNCH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    calls = []
    _herdr_fake_creating(monkeypatch, calls)
    triage.enqueue(tmp_path)
    triage.tick(cfg, deps, "/etc/targets.yaml")
    create = next(c for c in calls if c[:2] == ["tab", "create"])
    assert create == ["tab", "create", "--workspace", "w9", "--label", "triage",
                      "--cwd", triage._repo_dir(), "--no-focus"]
    run = next(c for c in calls if c[:2] == ["pane", "run"])
    assert run[2] == "w9:p2"
    assert "--triage-run" in run[3]
    assert "/etc/targets.yaml" in run[3]
    assert sys.executable in run[3]
    # The tab must close when the runner ends (the shell does not exit on its
    # own), or running() would read the finished sweep as still running.
    assert run[3].endswith("; exit")
    # request stays until the runner consumes it → claims stay paused
    assert triage.load_request(tmp_path) is not None


def test_tick_creates_the_system_workspace_on_first_use(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    monkeypatch.setattr(triage, "running", lambda: False)
    calls = []
    _herdr_fake_creating(monkeypatch, calls, workspace=H_NO_WS)
    triage.enqueue(tmp_path)
    triage.tick(cfg, FakeDeps(), "targets.yaml")
    assert ["workspace", "create", "--label", "agent-ops", "--cwd",
            triage._repo_dir(), "--no-focus"] in calls


def test_tick_forwards_dispatcher_env_to_the_runner(tmp_path, monkeypatch):
    """The herdr server's environment is the user unit's — no Telegram
    credentials — so the runner's env must be forwarded explicitly via the
    tab's --env pairs."""
    cfg = _sweep_cfg(tmp_path)
    monkeypatch.setattr(triage, "running", lambda: False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    calls = []
    _herdr_fake_creating(monkeypatch, calls)
    triage.enqueue(tmp_path)
    triage.tick(cfg, FakeDeps(), "targets.yaml")
    create = next(c for c in calls if c[:2] == ["tab", "create"])
    env = [create[i + 1] for i, a in enumerate(create) if a == "--env"]
    assert env == ["TELEGRAM_BOT_TOKEN=tok", "TELEGRAM_CHAT_ID=42",
                   f"AGENT_OPS_STATE_DIR={tmp_path}"]


def test_tick_launch_omits_unset_env_vars(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    calls = []
    _herdr_fake_creating(monkeypatch, calls)
    triage.enqueue(tmp_path)
    triage.tick(cfg, deps, "targets.yaml")  # no crash on the unset ones
    create = next(c for c in calls if c[:2] == ["tab", "create"])
    assert [create[i + 1] for i, a in enumerate(create) if a == "--env"] == [
        "TELEGRAM_BOT_TOKEN=tok"]


def test_tick_launch_failure_clears_and_notifies(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: False)
    _herdr_fake(monkeypatch, [])  # herdr server down: nothing can be created
    triage.enqueue(tmp_path)
    triage.tick(cfg, deps, "targets.yaml")
    assert triage.load_request(tmp_path) is None
    [(template, ctx)] = deps.notifier.sent
    assert template == "triage_report"
    assert ctx["lines"] == ["launch failed: herdr tab could not be created"]


def test_tick_pane_run_failure_clears_and_notifies(tmp_path, monkeypatch):
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: False)
    _herdr_fake_creating(monkeypatch, [], pane_run_ok=False)
    triage.enqueue(tmp_path)
    triage.tick(cfg, deps, "targets.yaml")
    assert triage.load_request(tmp_path) is None
    assert deps.notifier.sent[0][1]["lines"] == ["launch failed: herdr pane run failed"]


def test_tick_waits_when_capacity_full(tmp_path, monkeypatch):
    from dispatcher.state import Stage, TaskState, save
    cfg = _sweep_cfg(tmp_path)  # capacity=2
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: False)
    for issue in (1, 2):
        save(tmp_path, TaskState(
            issue=issue, target="a", stage=Stage.IMPLEMENT, slot=issue - 1,
            worktree="/w", branch="b", title="t", updated_at="now"))
    calls = []
    monkeypatch.setattr(triage.subprocess, "run",
                        lambda args, **k: calls.append(args))
    triage.enqueue(tmp_path)
    triage.tick(cfg, deps, "targets.yaml")
    assert calls == []
    assert triage.load_request(tmp_path) is not None


def test_pass_skips_claims_and_reduces_capacity_while_triage(tmp_path,
                                                             monkeypatch):
    from dispatcher import main as dmain
    cfg = _sweep_cfg(tmp_path)
    deps = FakeDeps()
    monkeypatch.setattr(triage, "running", lambda: True)
    monkeypatch.setattr(triage, "tick", lambda *a, **k: None)
    monkeypatch.setattr(dmain, "fetch_usage", lambda *a, **k: OK_USAGE)
    for fn in ("_handle_telegram", "_budget_edge", "_wake_ci", "_poll_prs",
               "_spawn_feedback", "_flush_done",
               "_prune_snapshots", "_apply_intents"):
        monkeypatch.setattr(dmain, fn, lambda *a, **k: None)
    seen_claim = {}
    seen_resume = {}

    def spy_claim(cfg_seen, *a, **k):
        seen_claim["capacity"] = cfg_seen.capacity

    def spy_resume(cfg_seen, *a, **k):
        seen_resume["capacity"] = cfg_seen.capacity

    monkeypatch.setattr(dmain, "_claim_new", spy_claim)
    monkeypatch.setattr(dmain, "_resume_woken", spy_resume)
    # running → pending → no claim; _resume_woken sees the reduced capacity
    dmain.run_pass(cfg, deps)
    assert seen_claim == {}
    assert seen_resume["capacity"] == cfg.capacity - 1

    seen_resume.clear()
    monkeypatch.setattr(triage, "running", lambda: False)
    # idle → claims run at full capacity; _resume_woken also sees full capacity
    dmain.run_pass(cfg, deps)
    assert seen_claim["capacity"] == cfg.capacity
    assert seen_resume["capacity"] == cfg.capacity
