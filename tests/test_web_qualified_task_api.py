"""Acceptance tests for ticket 04 (project-onboarding): the operator web API
tells apart repository-qualified issues that share a number.

Spec: specs/project-onboarding/spec.md requirements 6 and 24, scenario S57.
Seam: TASK_API -- task/queue HTTP routes, driven through web.app.create_app
over the real web.sources.Sources and a tmp state dir (no FakeSources).

Interface these tests define (design.md leaves the exact shapes open):
- repository_id is the "owner/name" repository with "/" written "~" (the
  spelling state.task_key already uses), so one path segment:
  `/api/task/{project}/{repository_id}/{issue}` for every task route that
  today reads `/api/task/{target}/{issue}` (detail, description, request,
  history, reply, answers, park, kill, cancel, retry, resume, run, artifacts).
  `{project}` is the Target name.
- Queue action bodies gain `repo` ("owner/name"): {"repo", "issue", ...}.
  Queue rows, task details, failure entries and history events carry `repo`.
- An old `/api/task/{target}/{issue}` route and a bare queue body keep working
  only when exactly one task/row has that number, and answer 409 naming the
  qualified candidates otherwise.
- Terminal snapshots are named `task-<state.task_key(target, issue, repo)>.txt`.
"""
import json

from fastapi.testclient import TestClient

from dispatcher import failures, intents, messages, state
from dispatcher import task_artifacts as artifacts
from tests.webfakes import HEADERS, make_config, make_target, make_task
from web.app import create_app
from web.sources import Sources

PROJECT = "shop"
FRONT, BACK = "example/shop-frontend", "example/shop-backend"
ISSUE = 12


def rid(repo):
    return repo.replace("/", "~")


def url(repo, tail=""):
    return f"/api/task/{PROJECT}/{rid(repo)}/{ISSUE}{tail}"


class FakeGitHub:
    def __init__(self):
        self.rows = {}
        self.writes = []

    def rank_rows(self, target):
        return self.rows.get(target.name, [])

    def issue_state(self, repo, number):
        return "OPEN"

    def set_boost(self, target, *args, **kw):
        self.writes.append(("boost", repr((args, kw))))

    def set_status(self, target, *args, **kw):
        self.writes.append(("status", repr((args, kw))))

    def add_label(self, target, *args, **kw):
        self.writes.append(("label", repr((args, kw))))


class FakeSessions:
    def capture_tail(self, target, issue, lines=25, repo=""):
        return ""

    def capture_history(self, target, issue, lines=2000, repo=""):
        return ""

    def is_alive(self, target, issue, repo=""):
        return False


def row(repo, **kw):
    return {"number": ISSUE, "title": f"issue of {repo}", "url": f"https://x/{repo}/12",
            "status": "Ready", "labels": ["auto"], "blocked": False,
            "score": 4.0, "boost": 0, "repo": repo, **kw}


def task(repo, **kw):
    return make_task(issue=ISSUE, target=PROJECT, repo=repo,
                     title=f"title of {repo}", **kw)


def rig(tmp_path, *, repos=(FRONT, BACK), tasks=True):
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(r) for r in repos]
    if tasks:
        for r in repos:
            state.save(tmp_path, task(r))
    cfg = make_config(tmp_path, targets=[make_target(PROJECT, FRONT)])
    sources = Sources(str(tmp_path), FakeSessions(), gh, systemctl=("true",))
    return gh, TestClient(create_app(cfg, sources))


def test_identical_issue_numbers_stay_distinct_through_queue_actions(tmp_path):
    """S57: an operator action on the frontend issue never touches backend #12."""
    gh, client = rig(tmp_path, tasks=False)
    state.save(tmp_path, task(FRONT))

    rows = client.get("/api/queue", headers=HEADERS).json()["targets"][0]["rows"]
    by_repo = {r["repo"]: r for r in rows}
    assert set(by_repo) == {FRONT, BACK}
    assert by_repo[FRONT]["in_flight"] is True
    assert by_repo[BACK]["in_flight"] is False

    r = client.post("/api/queue/boost", headers=HEADERS,
                    json={"repo": FRONT, "issue": ISSUE, "amount": 3})
    assert r.status_code == 200, r.text
    assert gh.writes and all(FRONT in w[1] and BACK not in w[1] for w in gh.writes)

    events = client.get("/api/history", headers=HEADERS).json()["events"]
    boost = [e for e in events if e["event"] == "queue-boost"]
    assert [e["repo"] for e in boost] == [FRONT]


def test_colliding_cards_open_separate_pages_threads_and_actions(tmp_path):
    gh, client = rig(tmp_path)
    messages.append(tmp_path, PROJECT, ISSUE, "for front", "operator", repo=FRONT)
    messages.append(tmp_path, PROJECT, ISSUE, "for back", "operator", repo=BACK)

    front = client.get(url(FRONT), headers=HEADERS)
    back = client.get(url(BACK), headers=HEADERS)
    assert front.status_code == back.status_code == 200
    assert (front.json()["title"], front.json()["repo"]) == (f"title of {FRONT}", FRONT)
    assert (back.json()["title"], back.json()["repo"]) == (f"title of {BACK}", BACK)
    assert "for front" in json.dumps(front.json()) and "for back" not in json.dumps(front.json())
    assert "for back" in json.dumps(back.json()) and "for front" not in json.dumps(back.json())

    assert client.post(url(BACK, "/reply"), headers=HEADERS,
                       json={"text": "hello back"}).status_code == 202
    assert client.post(url(FRONT, "/park"), headers=HEADERS).status_code == 202
    sent = sorted((i.action, i.repo) for i in intents.list_intents(tmp_path))
    assert sent == [("park", FRONT), ("reply", BACK)]
    assert "hello back" not in json.dumps(client.get(url(FRONT), headers=HEADERS).json())
    assert "hello back" in json.dumps(client.get(url(BACK), headers=HEADERS).json())


def test_ambiguous_bare_and_old_routes_ask_for_qualification(tmp_path):
    gh, client = rig(tmp_path)

    old = client.get(f"/api/task/{PROJECT}/{ISSUE}", headers=HEADERS)
    assert old.status_code == 409
    assert FRONT in old.json()["detail"] and BACK in old.json()["detail"]
    old_action = client.post(f"/api/task/{PROJECT}/{ISSUE}/park", headers=HEADERS)
    assert old_action.status_code == 409
    assert intents.list_intents(tmp_path) == []

    bare = client.post("/api/queue/boost", headers=HEADERS,
                       json={"issue": ISSUE, "amount": 1})
    assert bare.status_code == 409
    assert FRONT in bare.json()["detail"] and BACK in bare.json()["detail"]
    assert gh.writes == []

    # Once only one issue #12 is left, the old reference reaches it.
    state.delete(tmp_path, PROJECT, ISSUE, repo=BACK)
    gh.rows[PROJECT] = [row(FRONT)]
    sole = client.get(f"/api/task/{PROJECT}/{ISSUE}", headers=HEADERS)
    assert sole.status_code == 200 and sole.json()["repo"] == FRONT
    assert client.post("/api/queue/boost", headers=HEADERS,
                       json={"issue": ISSUE, "amount": 1}).status_code == 200

    # A legacy record whose repository is not proven is still reachable by
    # its old route while nothing else claims the number.
    state.delete(tmp_path, PROJECT, ISSUE, repo=FRONT)
    state.save(tmp_path, task(""))
    legacy = client.get(f"/api/task/{PROJECT}/{ISSUE}", headers=HEADERS)
    assert legacy.status_code == 200 and legacy.json()["title"] == "title of "
    # ... and so are its actions, not only its page.
    gh.rows[PROJECT] = []
    assert client.post(f"/api/task/{PROJECT}/{ISSUE}/park", headers=HEADERS).status_code == 202
    assert client.post(f"/api/task/{PROJECT}/{ISSUE}/kill", headers=HEADERS).status_code == 202
    assert sorted((i.action, i.repo) for i in intents.list_intents(tmp_path)) == [
        ("kill", ""), ("park", "")]


def test_history_failures_snapshots_and_artifacts_resolve_both_identities(tmp_path):
    gh, client = rig(tmp_path)
    for r in (FRONT, BACK):
        failures.write_quarantine(tmp_path, PROJECT, ISSUE, "example/dep", 99,
                                  f"fp-{rid(r)}", repo=r)
        (tmp_path / "snapshots").mkdir(exist_ok=True)
        (tmp_path / "snapshots" / f"task-{state.task_key(PROJECT, ISSUE, r)}.txt"
         ).write_text(f"terminal of {r}")
        wt = tmp_path / f"wt-{rid(r)}"
        (wt / ".agent").mkdir(parents=True)
        (wt / "preview.html").write_text(f"<h1>{r}</h1>")
        (wt / ".agent/artifacts.json").write_text(json.dumps(
            [{"id": "proto", "name": "Prototype", "path": "preview.html"}]))
        artifacts.collect(tmp_path, task(r, worktree=str(wt)), r, publish=False)

    quarantined = client.get("/api/failures", headers=HEADERS).json()["quarantined"]
    assert sorted((q["repo"], q["task_issue"], q["target"]) for q in quarantined) == [
        (BACK, ISSUE, PROJECT), (FRONT, ISSUE, PROJECT)]
    assert client.post(url(BACK, "/retry"), headers=HEADERS).status_code == 202
    assert [i.repo for i in intents.list_intents(tmp_path)] == [BACK]

    for r in (FRONT, BACK):
        hist = client.get(url(r, "/history"), headers=HEADERS)
        assert hist.json()["text"] == f"terminal of {r}"
        listing = client.get(url(r, "/artifacts"), headers=HEADERS).json()["items"]
        assert [a["id"] for a in listing] == ["proto"]
        assert rid(r) in listing[0]["url"]
        opened = client.get(listing[0]["url"], headers=HEADERS)
        assert f"<h1>{r}</h1>" in opened.text

    # A legacy record keeps resolving through the old identity.
    state.delete(tmp_path, PROJECT, ISSUE, repo=FRONT)
    state.delete(tmp_path, PROJECT, ISSUE, repo=BACK)
    state.save(tmp_path, task(""))
    (tmp_path / "snapshots" / f"task-{PROJECT}-{ISSUE}.txt").write_text("legacy terminal")
    gh.rows[PROJECT] = []
    assert client.get(f"/api/task/{PROJECT}/{ISSUE}/history",
                      headers=HEADERS).json()["text"] == "legacy terminal"


def test_openapi_types_publish_the_qualified_identity(tmp_path):
    _, client = rig(tmp_path)
    spec = client.get("/openapi.json", headers=HEADERS).json()
    assert "/api/task/{project}/{repository_id}/{issue}" in spec["paths"]
    schemas = spec["components"]["schemas"]
    assert "repo" in schemas["QueueRow"]["properties"]
    assert "repo" in schemas["TaskDetail"]["properties"]
    for body in ("BoostReq", "NextReq", "ReadyReq"):
        assert "repo" in schemas[body]["properties"]


class RecordingGitHub:
    """Records every cancel the dispatcher sends, with the repository it names."""

    def __init__(self):
        self.canceled = []
        self.released = []

    def release(self, target, issue, reason, *args, **kw):
        named = [kw.get("repo")] + [a for a in args if isinstance(a, str)]
        self.released.append((next((n for n in named if n), target.repo), issue))

    def cancel(self, target, issue, *args, **kw):
        named = [kw.get("repo")] + [a for a in args if isinstance(a, str)]
        self.canceled.append((next((n for n in named if n), target.repo), issue))

    def __getattr__(self, name):
        return lambda *a, **kw: None


def drain(tmp_path, gh, sessions=None, notifier=None):
    from dispatcher import main
    from tests.test_main import FakeSessions
    cfg = make_config(tmp_path, targets=[make_target(PROJECT, FRONT)])
    main._apply_intents(cfg, main.Deps(github=gh, sessions=sessions or FakeSessions(),
                                       notifier=notifier))


def repo_aware_sessions(alive):
    """FakeSessions whose end() takes the optional repo like the real Sessions,
    so a call that passes repo is recorded instead of dying on a TypeError."""
    from tests.test_main import FakeSessions

    class RepoAwareSessions(FakeSessions):
        def end(self, target, issue, repo=""):
            self.repo_end_calls = getattr(self, "repo_end_calls", []) + [(target, repo, issue)]
            super().end(target, issue)

    return RepoAwareSessions(alive=alive)


def test_qualified_retry_clears_only_its_own_quarantine(tmp_path):
    _, client = rig(tmp_path)
    for r in (FRONT, BACK):
        failures.write_quarantine(tmp_path, PROJECT, ISSUE, "example/dep", 99,
                                  f"fp-{rid(r)}", repo=r)
    failures.write_quarantine(tmp_path, PROJECT, ISSUE, "example/dep", 99,
                              "fp-legacy")

    assert client.post(url(BACK, "/retry"), headers=HEADERS).status_code == 202
    drain(tmp_path, RecordingGitHub())

    assert not failures.quarantine_path(tmp_path, PROJECT, ISSUE, BACK).exists()
    assert failures.quarantine_path(tmp_path, PROJECT, ISSUE, FRONT).exists()
    assert failures.quarantine_path(tmp_path, PROJECT, ISSUE).exists()


def test_qualified_cancel_closes_its_own_repository_issue(tmp_path):
    gh_web, client = rig(tmp_path)
    assert client.post(url(BACK, "/cancel"), headers=HEADERS).status_code == 202
    github = RecordingGitHub()
    drain(tmp_path, github)

    assert github.canceled == [(BACK, ISSUE)]
    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).stage == state.Stage.CANCELED
    assert state.load(tmp_path, PROJECT, ISSUE, repo=FRONT).stage != state.Stage.CANCELED


def test_qualified_kill_releases_only_its_own_repository_issue(tmp_path):
    from tests.test_main import FakeSessions
    _, client = rig(tmp_path)
    assert client.post(url(BACK, "/kill"), headers=HEADERS).status_code == 202
    github, sessions = RecordingGitHub(), repo_aware_sessions({ISSUE})
    drain(tmp_path, github, sessions)

    assert github.released == [(BACK, ISSUE)]
    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).stage == state.Stage.FAILED
    assert state.load(tmp_path, PROJECT, ISSUE, repo=FRONT).stage != state.Stage.FAILED
    # Its session label is qualified, so ending it cannot reach the sibling's.
    assert sessions.end_calls == [(PROJECT, ISSUE)]
    assert sessions.repo_end_calls == [(PROJECT, BACK, ISSUE)]


def test_qualified_cancel_ends_only_its_own_session(tmp_path):
    _, client = rig(tmp_path)
    assert client.post(url(BACK, "/cancel"), headers=HEADERS).status_code == 202
    sessions, github = repo_aware_sessions({ISSUE}), RecordingGitHub()
    drain(tmp_path, github, sessions)

    assert github.canceled == [(BACK, ISSUE)]
    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).stage == state.Stage.CANCELED
    assert state.load(tmp_path, PROJECT, ISSUE, repo=FRONT).stage != state.Stage.CANCELED
    assert sessions.end_calls == [(PROJECT, ISSUE)]
    assert sessions.repo_end_calls == [(PROJECT, BACK, ISSUE)]


def test_qualified_park_parks_and_ends_only_its_own_session(tmp_path):
    from tests.test_main import FakeNotifier
    _, client = rig(tmp_path)
    assert client.post(url(BACK, "/park"), headers=HEADERS).status_code == 202
    sessions, notifier = repo_aware_sessions({ISSUE}), FakeNotifier()
    drain(tmp_path, RecordingGitHub(), sessions, notifier)

    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).park
    assert not state.load(tmp_path, PROJECT, ISSUE, repo=FRONT).park
    assert sessions.end_calls == [(PROJECT, ISSUE)]
    assert sessions.repo_end_calls == [(PROJECT, BACK, ISSUE)]


class LiveSessions(FakeSessions):
    """A live session of the backend execution only."""

    def capture_tail(self, target, issue, lines=25, repo=""):
        return "LIVE of someone"

    def capture_history(self, target, issue, lines=2000, repo=""):
        return "LIVE of someone"

    def is_alive(self, target, issue, repo=""):
        return repo == BACK


def test_colliding_tasks_never_read_a_shared_session_or_snapshot(tmp_path):
    """A repository-less snapshot is no task's output, and a live session is
    read only through its own repository's label."""
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(FRONT), row(BACK)]
    for r in (FRONT, BACK):
        state.save(tmp_path, task(r))
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / f"task-{PROJECT}-{ISSUE}.txt").write_text("shared writer output")
    cfg = make_config(tmp_path, targets=[make_target(PROJECT, FRONT)])

    dead = TestClient(create_app(cfg, Sources(str(tmp_path), FakeSessions(), gh,
                                              systemctl=("true",))))
    for r in (FRONT, BACK):
        assert dead.get(url(r, "/history"), headers=HEADERS).json()["text"] == ""

    live = TestClient(create_app(cfg, Sources(str(tmp_path), LiveSessions(), gh,
                                              systemctl=("true",))))
    assert live.get(url(BACK, "/history"), headers=HEADERS).json()["text"] == "LIVE of someone"
    assert live.get(url(FRONT, "/history"), headers=HEADERS).json()["text"] == ""
    assert "LIVE of someone" not in live.get(url(FRONT), headers=HEADERS).text

    # A restart after the frontend task is removed must still withhold the
    # unqualified snapshot and address the backend session by its own identity.
    state.delete(tmp_path, PROJECT, ISSUE, repo=FRONT)
    gh.rows[PROJECT] = [row(BACK)]
    fresh = TestClient(create_app(cfg, Sources(str(tmp_path), FakeSessions(), gh,
                                               systemctl=("true",))))
    assert fresh.get(url(BACK, "/history"), headers=HEADERS).json()["text"] == ""
    survivor = TestClient(create_app(cfg, Sources(str(tmp_path), LiveSessions(), gh,
                                                  systemctl=("true",))))
    assert survivor.get(url(BACK, "/history"), headers=HEADERS).json()["text"] == "LIVE of someone"
