"""Acceptance tests for ticket 05: answers files are review artifacts."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from dispatcher import task_artifacts as artifacts
from dispatcher.state import Stage
from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app

REVIEW = '.agent/review-answers.json'
QUESTIONNAIRE = '.agent/questionnaire-answers.json'
TERMINAL = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def worktree(tmp_path, **files):
    wt = tmp_path / 'worktree'
    (wt / '.agent').mkdir(parents=True)
    for path, text in files.items():
        (wt / path).write_text(text)
    return wt


def collect(tmp_path, wt, **kw):
    state = tmp_path / 'state'
    task = make_task(issue=412, worktree=str(wt), **kw)
    artifacts.collect(state, task, '', publish=False, now=TERMINAL)
    return state, task


def test_both_answers_files_are_registered_with_snapshot_and_listed_local(tmp_path):
    wt = worktree(tmp_path)
    (wt / REVIEW).write_text('{"stage": "plan"}')
    (wt / QUESTIONNAIRE).write_text('{"stage": "spec"}')
    state, task = collect(tmp_path, wt)
    items = {i.id: i for i in artifacts.read(state, 'alpha', 412).items}
    assert items['review-answers'].name == 'Review answers'
    assert items['review-answers'].path == REVIEW
    assert items['questionnaire-answers'].path == QUESTIONNAIRE
    assert artifacts.content_path(state, 'alpha', 412, 'review-answers').read_text() == '{"stage": "plan"}'
    assert artifacts.content_path(state, 'alpha', 412, 'questionnaire-answers').read_text() == '{"stage": "spec"}'
    fake = FakeSources(state)
    fake.tasks_list = [task]   # an active task is known to the console through its sources
    client = TestClient(create_app(make_config(state), fake))
    body = client.get('/api/task/alpha/412/artifacts', headers=HEADERS).json()
    status = {i['id']: i['status'] for i in body['items']}
    assert status['review-answers'] == status['questionnaire-answers'] == 'local'


def test_manifest_entry_for_same_path_is_kept_without_duplicate(tmp_path):
    wt = worktree(tmp_path)
    (wt / REVIEW).write_text('{}')
    (wt / QUESTIONNAIRE).write_text('{}')
    (wt / '.agent/artifacts.json').write_text(json.dumps(
        [{'id': 'my-answers', 'name': 'My answers', 'path': REVIEW}]))
    state, _ = collect(tmp_path, wt)
    items = artifacts.read(state, 'alpha', 412).items
    ids = [i.id for i in items]
    paths = [i.path for i in items]
    assert len(set(ids)) == len(ids) and len(set(paths)) == len(paths)
    mine = [i for i in items if i.path == REVIEW]
    assert [(i.id, i.name) for i in mine] == [('my-answers', 'My answers')]
    # The other answers file is still registered automatically.
    assert 'questionnaire-answers' in ids


def test_review_artifacts_expire_after_seven_days(tmp_path):
    wt = worktree(tmp_path)
    (wt / REVIEW).write_text('{}')
    (wt / '.agent/artifacts.json').write_text(json.dumps(
        [{'id': 'review-answers', 'name': 'Review answers', 'path': REVIEW}]))
    state, _ = collect(tmp_path, wt, stage=Stage.DONE, terminal_at=TERMINAL.isoformat())
    artifacts.cleanup(state, now=datetime(2026, 10, 27, 9, 59, tzinfo=timezone.utc))
    assert artifacts.content_path(state, 'alpha', 412, 'review-answers')
    assert not artifacts.read(state, 'alpha', 412).expired
    artifacts.cleanup(state, now=datetime(2026, 10, 27, 10, 1, tzinfo=timezone.utc))
    assert artifacts.content_path(state, 'alpha', 412, 'review-answers') is None
    assert artifacts.read(state, 'alpha', 412).expired
