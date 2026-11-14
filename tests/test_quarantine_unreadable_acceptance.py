"""#87 acceptance: an unreadable quarantine record blocks its issue and says so.

A record the dispatcher cannot read keeps its issue unclaimed, so the console
must list it, explicitly marked unreadable, and let the operator retry it —
and the dispatcher must keep blocking it instead of raising mid-pass.
"""
import json

import pytest
from fastapi.testclient import TestClient

from dispatcher import failures
from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources

UNREADABLE = {73: b'{"blocker_issue": \xff\xfe}',  # torn write: invalid UTF-8
              74: b"null", 75: b"[1, 2]", 76: b"{not json"}


class _NullGitHub:
    def rank_rows(self, _): return []

    def issue_state(self, _repo, _issue): return "OPEN"


class _NullSessions:
    def is_alive(self, _): return False


def _state(tmp_path):
    d = tmp_path / "quarantine"
    d.mkdir(parents=True)
    for issue, raw in UNREADABLE.items():
        (d / f"alpha-{issue}.json").write_bytes(raw)
    failures.write_quarantine(tmp_path, "alpha", 77, "r", 5, "fp")
    cfg = make_config(tmp_path)
    return TestClient(create_app(cfg, Sources(cfg.state_dir, _NullSessions(),
                                              _NullGitHub())))


def test_failures_lists_unreadable_records_as_unreadable(tmp_path):
    client = _state(tmp_path)
    resp = client.get("/api/failures", headers=HEADERS)
    assert resp.status_code == 200
    rows = {q["task_issue"]: q for q in resp.json()["quarantined"]}
    assert sorted(rows) == [73, 74, 75, 76, 77]
    for issue in UNREADABLE:
        assert rows[issue]["repository"] == "alpha"
        assert rows[issue]["readable"] is False
        assert rows[issue]["blocker_open"] is None
    assert rows[77]["readable"] is True


def test_retry_route_accepts_an_unreadable_record(tmp_path):
    client = _state(tmp_path)
    resp = client.post("/api/task/alpha/74/retry", headers=HEADERS)
    assert resp.status_code == 202, resp.text


@pytest.mark.parametrize("issue", sorted(UNREADABLE))
def test_dispatcher_keeps_blocking_an_unreadable_record(tmp_path, issue):
    _state(tmp_path)
    assert failures.check_quarantine(tmp_path, _NullGitHub(), "alpha", issue) is True
    record = failures.quarantine_path(tmp_path, "alpha", issue)
    assert record.read_bytes() == UNREADABLE[issue]


# --- the shared reader: a record gone by its read blocks nothing ------------

def _vanish_on_read(monkeypatch, path):
    """`path` exists until something opens it, then is gone: a retry or a
    closed blocker deleted it between the listing (or `exists()`) and the
    read."""
    import builtins
    from pathlib import Path

    def wrap(orig, path_of):
        def opened(*a, **kw):
            if str(path_of(*a)) == str(path):
                path.unlink(missing_ok=True)
            return orig(*a, **kw)
        return opened

    for name in ("read_text", "read_bytes", "open"):
        monkeypatch.setattr(Path, name,
                            wrap(getattr(Path, name), lambda p, *_: p))
    monkeypatch.setattr(builtins, "open",
                        wrap(builtins.open, lambda f, *_: f))


def test_check_quarantine_on_a_record_gone_by_its_read_unblocks(
        tmp_path, monkeypatch):
    failures.write_quarantine(tmp_path, "alpha", 79, "r", 5, "fp")
    _vanish_on_read(monkeypatch,
                    failures.quarantine_path(tmp_path, "alpha", 79))
    assert failures.check_quarantine(tmp_path, _NullGitHub(), "alpha", 79) is False


# --- the dispatcher: an unreadable record never crashes a pass --------------

def _two_repository_cfg(tmp_path):
    from dataclasses import replace
    from tests.configfakes import with_board
    from tests.test_main import cfg
    c = cfg(tmp_path)
    base = c.repositories[0]

    def repository(name, number):
        return with_board(replace(
            base, name=name, repo=f"jesdi/{name}", github_id=number,
            clone_path=str(tmp_path / f"{name}_repo"),
            worktrees_path=str(tmp_path / f"{name}_repo.worktrees"),
            project=replace(base.project, name=name)), number=number)

    return replace(c, repositories=[repository("alpha", 1),
                                    repository("beta", 2)])


def _write_record(state_dir, issue, raw):
    d = failures.quarantine_path(state_dir, "alpha", issue).parent
    d.mkdir(parents=True, exist_ok=True)
    (d / f"alpha-{issue}.json").write_bytes(raw)
    return d / f"alpha-{issue}.json"


def test_an_unreadable_record_never_crashes_the_claim_pass(tmp_path, monkeypatch):
    import dispatcher.main as main
    from dispatcher.github import Candidate
    from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, deps,
                                 patch_usage, patch_workspace)
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = _two_repository_cfg(tmp_path)
    record = _write_record(c.state_dir, 74, b"null")
    gh = FakeGitHub(cands_by_repository={
        "alpha": [Candidate(74, "Quarantined", "u74")],
        "beta": [Candidate(10, "Good", "u10")]})
    notifier = FakeNotifier()
    main.run_pass(c, deps(gh, FakeSessions(), notifier))
    assert gh.claimed == [10]
    assert record.exists()
    assert gh.created_issues == [], "no pass crash was reported"


def test_retrying_an_unreadable_record_clears_it(tmp_path, monkeypatch):
    import dispatcher.main as main
    from dispatcher import intents
    from tests.test_main import (FakeGitHub, FakeSessions, deps, patch_usage,
                                 patch_workspace)
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = _two_repository_cfg(tmp_path)
    record = _write_record(c.state_dir, 75, b"[1, 2]")
    intents.write_intent(c.state_dir, "retry", "alpha", 75, {}, "op", 1)
    gh = FakeGitHub()
    main.run_pass(c, deps(gh, FakeSessions()))
    assert not record.exists()
    assert gh.created_issues == [], "no pass crash was reported"
    assert failures.check_quarantine(c.state_dir, _NullGitHub(), "alpha", 75) is False


# --- GET /api/failures against the real Sources -----------------------------

class _CountingGitHub(_NullGitHub):
    def __init__(self):
        self.lookups = []

    def issue_state(self, repo, issue):
        self.lookups.append((repo, issue))
        return "OPEN"


def _client(tmp_path, github=None):
    cfg = make_config(tmp_path)
    return TestClient(create_app(cfg, Sources(cfg.state_dir, _NullSessions(),
                                              github or _NullGitHub())))


def _quarantined(client):
    resp = client.get("/api/failures", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()["quarantined"]


def test_a_readable_record_is_unchanged_except_for_the_flag(tmp_path):
    failures.write_quarantine(tmp_path, "alpha", 77, "jesdi/alpha", 5, "fp")
    written = json.loads(
        failures.quarantine_path(tmp_path, "alpha", 77).read_text())["created_at"]
    assert _quarantined(_client(tmp_path)) == [{
        "repository": "alpha", "task_issue": 77, "blocker_repo": "jesdi/alpha",
        "blocker_issue": 5, "fingerprint": "fp", "created_at": written,
        "blocker_open": True, "readable": True}]


def test_an_empty_object_is_a_readable_record(tmp_path):
    _write_record(tmp_path, 78, b"{}")
    entry, = _quarantined(_client(tmp_path))
    assert (entry["task_issue"], entry["readable"], entry["blocker_open"]) == (
        78, True, None)


def test_no_blocker_lookup_for_an_unreadable_record(tmp_path):
    _write_record(tmp_path, 74, b"null")
    gh = _CountingGitHub()
    entry, = _quarantined(_client(tmp_path, gh))
    assert gh.lookups == []
    assert (entry["repository"], entry["task_issue"]) == ("alpha", 74)
    assert entry["blocker_open"] is None


def test_an_unreadable_record_with_an_unparseable_name_is_not_listed(tmp_path):
    d = tmp_path / "quarantine"
    d.mkdir(parents=True)
    (d / "broken.json").write_bytes(b"null")
    failures.write_quarantine(tmp_path, "alpha", 77, "r", 5, "fp")
    rows = _quarantined(_client(tmp_path))
    assert [(q["task_issue"], q["readable"]) for q in rows] == [(77, True)]


def test_a_record_gone_before_its_read_is_not_listed(tmp_path, monkeypatch):
    failures.write_quarantine(tmp_path, "alpha", 79, "r", 5, "fp")
    failures.write_quarantine(tmp_path, "alpha", 77, "r", 5, "fp")
    _vanish_on_read(monkeypatch, failures.quarantine_path(tmp_path, "alpha", 79))
    rows = _quarantined(_client(tmp_path))
    assert [(q["task_issue"], q["readable"]) for q in rows] == [(77, True)]


# --- GET /api/board: the claim forecast skips an unreadable head -------------

def test_the_claim_forecast_skips_an_unreadable_head(tmp_path):
    from datetime import datetime, timezone
    from tests.usagefakes import session_usage

    class _QueueGitHub(_NullGitHub):
        def rank_rows(self, repositories):
            return [{"repository": "alpha", "number": n, "title": f"t{n}",
                     "url": "u", "status": "Ready", "labels": ["auto"],
                     "blocked": False, "score": 2.0, "boost": 0}
                    for n in (212, 213)]

    class _UsageSources(Sources):
        def usage(self, routed):
            return {"anthropic": session_usage(0.5)}

    (tmp_path / "pass.json").write_text(json.dumps({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "interval_minutes": 10}))
    _write_record(tmp_path, 212, b"{not json")
    cfg = make_config(tmp_path)
    client = TestClient(create_app(cfg, _UsageSources(
        cfg.state_dir, _NullSessions(), _QueueGitHub())))
    nc = client.get("/api/board", headers=HEADERS).json()["next_claim"]
    assert (nc["verdict"], nc["next_issue"]) == ("will-claim", 213)


# --- a wrong-typed record is unreadable: blocks, listed, retryable ----------

WRONG_TYPED = {80: {"blocker_issue": "x"}, 81: {"fingerprint": None},
               82: {"blocker_issue": True}, 83: {"task_issue": "83"},
               84: {"blocker_repo": 5}, 85: {"created_at": 1}}


def _wrong_typed(state_dir):
    """Each record is well formed but for one field, so only the type check
    stands between it and a blocker lookup."""
    for issue, bad in WRONG_TYPED.items():
        rec = {"task_issue": issue, "blocker_repo": "r", "blocker_issue": 5,
               "fingerprint": "fp", "created_at": "c", **bad}
        _write_record(state_dir, issue, json.dumps(rec).encode())


@pytest.mark.parametrize("issue", sorted(WRONG_TYPED))
def test_dispatcher_blocks_a_wrong_typed_record_without_a_lookup(tmp_path, issue):
    _wrong_typed(tmp_path)
    gh = _CountingGitHub()
    assert failures.check_quarantine(tmp_path, gh, "alpha", issue) is True
    assert gh.lookups == []
    assert failures.quarantine_path(tmp_path, "alpha", issue).exists()


def test_failures_lists_a_wrong_typed_record_as_unreadable(tmp_path):
    _wrong_typed(tmp_path)
    d = tmp_path / "quarantine"
    (d / "broken.json").write_text(json.dumps({"blocker_issue": "x"}))
    gh = _CountingGitHub()
    client = _client(tmp_path, gh)
    rows = {q["task_issue"]: q for q in _quarantined(client)}
    assert sorted(rows) == sorted(WRONG_TYPED)
    for row in rows.values():
        assert (row["repository"], row["readable"], row["blocker_open"],
                row["blocker_repo"], row["blocker_issue"], row["fingerprint"],
                row["created_at"]) == ("alpha", False, None, "", 0, "", "")
    assert gh.lookups == []
    resp = client.post("/api/task/alpha/80/retry", headers=HEADERS)
    assert resp.status_code == 202, resp.text


def test_retrying_a_wrong_typed_record_clears_it(tmp_path, monkeypatch):
    import dispatcher.main as main
    from dispatcher import intents
    from tests.test_main import (FakeGitHub, FakeSessions, deps, patch_usage,
                                 patch_workspace)
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = _two_repository_cfg(tmp_path)
    record = _write_record(c.state_dir, 81, b'{"fingerprint": null}')
    intents.write_intent(c.state_dir, "retry", "alpha", 81, {}, "op", 1)
    main.run_pass(c, deps(FakeGitHub(), FakeSessions()))
    assert not record.exists()
