"""Ticket 04 fixer 2: colliding issue numbers across archived history, backlog
resolution, queued mail and the snapshot writer."""
import json

from fastapi.testclient import TestClient

from dispatcher import messages, state
from dispatcher.sessions import Sessions
from tests.test_web_qualified_task_api import (BACK, FRONT, HEADERS, ISSUE, PROJECT,
                                               FakeGitHub, FakeSessions, row, rid, url)
from tests.webfakes import make_config, make_target, make_task
from web.app import create_app
from web.sources import Sources


def client(tmp_path, gh=None):
    cfg = make_config(tmp_path, targets=[make_target(PROJECT, FRONT)])
    return TestClient(create_app(cfg, Sources(str(tmp_path), FakeSessions(), gh or FakeGitHub(),
                                              systemctl=("true",))))


def test_archived_collision_asks_for_qualification(tmp_path):
    for r in (FRONT, BACK):
        state.archive(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=r,
                                          stage=state.Stage.DONE))
    c = client(tmp_path)
    assert c.get(f"/api/task/{PROJECT}/{ISSUE}", headers=HEADERS).status_code == 409
    assert c.get(url(BACK), headers=HEADERS).status_code == 200


def test_bare_cancel_sees_a_colliding_backlog_issue(tmp_path):
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(FRONT), row(BACK)]
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=FRONT))
    res = client(tmp_path, gh).post(f"/api/task/{PROJECT}/{ISSUE}/cancel", headers=HEADERS)
    assert res.status_code == 409


def test_board_counts_queued_mail_per_repository(tmp_path):
    for r in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=r))
    messages.append(tmp_path, PROJECT, ISSUE, "hi", "op", repo=BACK)
    cards = {c["repo"]: c["undelivered_messages"]
             for col in client(tmp_path).get("/api/board/snapshot", headers=HEADERS)
             .json()["columns"] for c in col["cards"]}
    assert cards == {FRONT: 0, BACK: 1}


def test_snapshot_writer_names_the_repository(tmp_path):
    class Pane(Sessions):
        def capture_history(self, target, issue, lines=2000, repo=""):
            return "BACK terminal"

    Pane(state_dir=tmp_path)._snapshot(PROJECT, ISSUE, BACK)
    assert (tmp_path / "snapshots" / f"task-{rid(BACK)}~{PROJECT}-{ISSUE}.txt").read_text() \
        == "BACK terminal"


def test_active_and_archived_collision_rejects_every_bare_action(tmp_path):
    state.archive(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=FRONT,
                                      stage=state.Stage.DONE))
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK))
    c = client(tmp_path)
    assert c.get(f"/api/task/{PROJECT}/{ISSUE}", headers=HEADERS).status_code == 409
    for action in ("park", "kill", "cancel"):
        result = c.post(f"/api/task/{PROJECT}/{ISSUE}/{action}", headers=HEADERS)
        assert result.status_code == 409
        assert FRONT in result.text and BACK in result.text
    assert not list((tmp_path / "intents").glob("*.json"))
    assert c.get(url(BACK), headers=HEADERS).json()["repo"] == BACK


def test_same_active_and_archived_identity_is_not_ambiguous(tmp_path):
    state.archive(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK,
                                      stage=state.Stage.DONE))
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK))
    assert client(tmp_path).get(f"/api/task/{PROJECT}/{ISSUE}", headers=HEADERS).status_code == 200


def test_legacy_snapshot_cannot_gain_an_owner_after_task_removal(tmp_path):
    for repo in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=repo))
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / f"task-{PROJECT}-{ISSUE}.txt").write_text("FRONT historical output")
    assert client(tmp_path).get(url(BACK, "/history"), headers=HEADERS).json()["text"] == ""
    state.delete(tmp_path, PROJECT, ISSUE, repo=FRONT)
    # A fresh reader must also withhold it, across process restarts.
    assert client(tmp_path).get(url(BACK, "/history"), headers=HEADERS).json()["text"] == ""
    (snapshots / f"task-{state.task_key(PROJECT, ISSUE, BACK)}.txt").write_text("BACK output")
    assert client(tmp_path).get(url(BACK, "/history"), headers=HEADERS).json()["text"] == "BACK output"


def test_board_scores_models_and_tracks_are_qualified(tmp_path):
    from dataclasses import replace
    from tests.test_web_pinned_views import policy
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(FRONT, score=1), row(BACK, score=9)]
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=FRONT,
                                   track="frontend", picks={"implement": "claude-opus-5"}))
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK,
                                   picks={"implement": "claude-fable-5"}))
    cfg = replace(make_config(tmp_path, targets=[make_target(PROJECT, FRONT)]), models=policy())
    c = TestClient(create_app(cfg, Sources(str(tmp_path), FakeSessions(), gh, systemctl=("true",))))
    for route in ("/api/board", "/api/board/snapshot"):
        cards = {card["repo"]: card for col in c.get(route, headers=HEADERS).json()["columns"]
                 for card in col["cards"]}
        assert cards[FRONT]["model"] == "anthropic/claude-opus-5"
        assert cards[BACK]["model"] == "anthropic/claude-fable-5"
        assert cards[FRONT]["pinned_track"] == "frontend"
        assert cards[BACK]["pinned_track"] == ""
        if route == "/api/board":
            assert cards[FRONT]["score"] == 1
            assert cards[BACK]["score"] == 9
            assert cards[FRONT]["admission"] != cards[BACK]["admission"]


def test_unknown_qualified_cancel_never_queues_or_closes_issue(tmp_path):
    from dispatcher import intents, main
    from tests.test_web_qualified_task_api import RecordingGitHub, drain
    gh = FakeGitHub()
    unrelated = "example/unrelated"
    result = client(tmp_path, gh).post(url(unrelated, "/cancel"), headers=HEADERS)
    assert result.status_code == 404
    assert not list((tmp_path / "intents").glob("*.json"))
    # Even an intent written by an older caller is revalidated before effects.
    gh = RecordingGitHub()
    gh.rank_rows = lambda target: [row(FRONT)]
    intents.write_intent(tmp_path, "cancel", PROJECT, ISSUE, {}, "op", 1, repo=unrelated)
    drain(tmp_path, gh)
    assert gh.canceled == []
    assert not any(e.get("event") == "canceled" for e in main.eventlog.read_tail(tmp_path))


def test_qualified_backlog_cancel_rechecks_and_uses_queue_repository(tmp_path):
    from dispatcher import intents
    from tests.test_web_qualified_task_api import RecordingGitHub, drain, repo_aware_sessions
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(BACK)]
    assert client(tmp_path, gh).post(url(BACK, "/cancel"), headers=HEADERS).status_code == 202
    writer = RecordingGitHub()
    writer.rank_rows = lambda target: [row(BACK)]
    drain(tmp_path, writer, repo_aware_sessions(set()))
    assert writer.canceled == [(BACK, ISSUE)]


def test_park_ends_only_its_own_session_and_reply_ends_none(tmp_path):
    from dispatcher import intents
    from tests.test_main import FakeNotifier
    from tests.test_web_qualified_task_api import RecordingGitHub, drain, repo_aware_sessions
    for repo in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=repo))
    sessions = repo_aware_sessions({ISSUE})
    notifier = FakeNotifier()
    intents.write_intent(tmp_path, "park", PROJECT, ISSUE, {}, "op", 1, repo=BACK)
    drain(tmp_path, RecordingGitHub(), sessions, notifier)
    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).park == state.PARK_HUMAN
    assert not state.load(tmp_path, PROJECT, ISSUE, repo=FRONT).park
    assert sessions.end_calls == [(PROJECT, ISSUE)]
    assert notifier.contexts[0][1]["url"] == f"https://github.com/{BACK}/issues/{ISSUE}"
    assert notifier.contexts[0][1]["note"].startswith("parked by operator")
    assert not list((tmp_path / "snapshots").glob("*"))
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK,
                                   stage=state.Stage.AWAITING_PLAN_REVIEW))
    intents.write_intent(tmp_path, "reply", PROJECT, ISSUE, {"text": "continue"}, "op", 1, repo=BACK)
    drain(tmp_path, RecordingGitHub(), sessions, notifier)
    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).park == state.PARK_WAKE
    assert sessions.end_calls == [(PROJECT, ISSUE)] * 2
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK, park=state.PARK_LOGIN))
    intents.write_intent(tmp_path, "reply", PROJECT, ISSUE, {"text": "code"}, "op", 1, repo=BACK)
    drain(tmp_path, RecordingGitHub(), sessions, notifier)
    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).park == state.PARK_LOGIN
    assert sessions.end_calls == [(PROJECT, ISSUE)] * 2


def test_a_launch_record_is_durable_and_never_transfers_on_deletion(tmp_path):
    for repo in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=repo))
    state.delete(tmp_path, PROJECT, ISSUE, repo=FRONT)
    state.record_session_owner(tmp_path, PROJECT, ISSUE, FRONT)
    assert not state.owns_task_session(tmp_path, PROJECT, ISSUE, BACK)
    assert state.owns_task_session(tmp_path, PROJECT, ISSUE, FRONT)


def test_archived_identity_participates_in_bare_backlog_actions(tmp_path):
    state.archive(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=FRONT,
                                      stage=state.Stage.DONE))
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(BACK)]
    c = client(tmp_path, gh)
    for action, body in (("cancel", {}), ("reply", {"text": "hi"})):
        response = c.post(f"/api/task/{PROJECT}/{ISSUE}/{action}", json=body, headers=HEADERS)
        assert response.status_code == 409
        assert FRONT in response.text and BACK in response.text
    gh.rows[PROJECT] = []
    c = client(tmp_path, gh)
    assert c.post(f"/api/task/{PROJECT}/{ISSUE}/reply", json={"text": "hi"},
                  headers=HEADERS).status_code == 202
    assert json.loads(next((tmp_path / "intents").glob("*.json")).read_text())["repo"] == FRONT


def test_operator_terminal_events_retain_repository(tmp_path):
    from dispatcher import eventlog, intents
    from tests.test_web_qualified_task_api import RecordingGitHub, drain
    for action, event in (("kill", "failed"), ("cancel", "canceled")):
        state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK))
        intents.write_intent(tmp_path, action, PROJECT, ISSUE, {}, "op", 1, repo=BACK)
        drain(tmp_path, RecordingGitHub())
        assert [e for e in eventlog.read_tail(tmp_path) if e["event"] == event][-1]["repo"] == BACK


def test_board_claims_and_cycle_duration_are_repository_qualified(tmp_path):
    from dispatcher import eventlog
    from web.read_model import median_cycle_seconds
    events = []
    for repo, day in ((FRONT, "01"), (BACK, "15")):
        state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=repo))
        eventlog.append_event(tmp_path, "claimed", target=PROJECT, issue=ISSUE, repo=repo)
        events.append({"event": "claimed", "target": PROJECT, "issue": ISSUE, "repo": repo,
                       "ts": f"2026-10-{day}T00:00:00Z"})
    (tmp_path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    for route in ("/api/board", "/api/board/snapshot"):
        cards = {card["repo"]: card for col in client(tmp_path).get(route, headers=HEADERS)
                 .json()["columns"] for card in col["cards"]}
        assert cards[FRONT]["claimed_at"] == events[0]["ts"]
        assert cards[BACK]["claimed_at"] == events[1]["ts"]
    events.append({"event": "merged", "target": PROJECT, "issue": ISSUE, "repo": BACK,
                   "ts": "2026-10-16T00:00:00Z"})
    assert median_cycle_seconds(events) == 86400


def test_qualified_resume_waits_without_touching_a_shared_session(tmp_path, monkeypatch):
    from dispatcher import main
    from tests.test_web_qualified_task_api import RecordingGitHub, repo_aware_sessions
    for repo in (FRONT, BACK):
        state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=repo,
                                      park=state.PARK_WAKE if repo == BACK else ""))
    cfg = make_config(tmp_path, targets=[make_target(PROJECT, FRONT)])
    sessions = repo_aware_sessions({ISSUE})
    deps = main.Deps(github=RecordingGitHub(), sessions=sessions, notifier=None)
    def forbidden_launch(*args):
        raise AssertionError("must not launch into a shared session")
    monkeypatch.setattr(main, "_wake_launch", forbidden_launch)
    main._resume_woken(cfg, deps, None, None)
    assert state.load(tmp_path, PROJECT, ISSUE, repo=BACK).park == state.PARK_WAKE
    assert sessions.end_calls == []
    # Even after inventory removal a surviving unowned live session blocks launch.
    state.delete(tmp_path, PROJECT, ISSUE, repo=FRONT)
    main._resume_woken(cfg, deps, None, None)
    assert sessions.end_calls == []


def test_archived_sibling_does_not_block_a_qualified_resume(tmp_path, monkeypatch):
    from dispatcher import main
    from tests.test_web_qualified_task_api import RecordingGitHub, repo_aware_sessions
    state.archive(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=FRONT,
                                      stage=state.Stage.DONE))
    state.save(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=BACK, park=state.PARK_WAKE))
    cfg = make_config(tmp_path, targets=[make_target(PROJECT, FRONT)])
    deps = main.Deps(github=RecordingGitHub(), sessions=repo_aware_sessions(set()), notifier=None)
    launched = []
    def wake_launch(cfg, deps, target, task, *args):
        launched.append(task.repo)
        return None, ""
    monkeypatch.setattr(main, "_wake_launch", wake_launch)
    main._resume_woken(cfg, deps, None, None)
    assert launched == [BACK]
    assert not list(tmp_path.glob("wake-blocked-*"))


def test_bare_run_asks_for_qualification_when_an_archived_identity_collides(tmp_path):
    state.archive(tmp_path, make_task(issue=ISSUE, target=PROJECT, repo=FRONT,
                                      stage=state.Stage.DONE))
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(BACK)]
    c = client(tmp_path, gh)
    result = c.post(f"/api/task/{PROJECT}/{ISSUE}/run", json={}, headers=HEADERS)
    assert result.status_code == 409
    assert FRONT in result.text and BACK in result.text
    assert c.post(url(BACK, "/run"), json={}, headers=HEADERS).status_code == 200


def test_bare_run_sees_a_colliding_backlog_row_that_is_not_runnable(tmp_path):
    gh = FakeGitHub()
    gh.rows[PROJECT] = [row(FRONT, blocked=True), row(BACK)]
    result = client(tmp_path, gh).post(f"/api/task/{PROJECT}/{ISSUE}/run", json={},
                                       headers=HEADERS)
    assert result.status_code == 409
    assert FRONT in result.text and BACK in result.text
