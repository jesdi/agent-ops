"""Acceptance tests for ticket 05: answers files are review artifacts.

Since #282 the automatic answers artifacts are captured answered pages
(tests/test_answered_page_capture_acceptance.py, which also covers their
expiry); a bare answers file is no longer collected."""
import json
from datetime import datetime, timezone

from dispatcher import task_artifacts as artifacts
from tests.webfakes import make_task

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


def test_a_bare_answers_file_is_not_registered(tmp_path):
    # #282: the answers artifacts are the answered pages the drain captures;
    # an answers file with no `submitted` in the worktree is not collected.
    wt = worktree(tmp_path)
    (wt / REVIEW).write_text('{"stage": "plan"}')
    (wt / QUESTIONNAIRE).write_text('{"stage": "spec", "submitted": null}')
    state, _ = collect(tmp_path, wt)
    ids = [i.id for i in artifacts.read(state, 'alpha', 412).items]
    assert 'review-answers' not in ids
    assert 'questionnaire-answers' not in ids


def test_manifest_entry_for_an_answers_file_stays_a_plain_copy(tmp_path):
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
    assert artifacts.content_path(state, 'alpha', 412, 'my-answers').read_text() == '{}'
    # The bare questionnaire answers file is not registered (#282).
    assert 'questionnaire-answers' not in ids
