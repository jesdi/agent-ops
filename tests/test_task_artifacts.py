import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from dispatcher import spec_publish, task_artifacts as artifacts
from dispatcher.state import Stage, load, save, load_archived
from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def setup(tmp_path):
    wt = tmp_path / 'worktree'
    (wt / '.agent').mkdir(parents=True)
    (wt / 'preview.html').write_text('<h1>Prototype</h1><button onclick="this.textContent=42">Try</button>')
    (wt / 'spec.md').write_text('# Spec\nA reviewed design')
    (wt / '.agent/artifacts.json').write_text(json.dumps([
        {'id': 'prototype', 'name': 'Prototype', 'path': 'preview.html'},
    ]))
    task = make_task(worktree=str(wt), spec_path='spec.md', updated_at=NOW.isoformat())
    state = tmp_path / 'state'
    return state, task


def test_stable_url_latest_content_and_cross_session_retention(tmp_path):
    state, task = setup(tmp_path)
    artifacts.collect(state, task, 'owner/repo', publish=False, now=NOW)
    (Path(task.worktree) / 'preview.html').write_text('<h1>Revised</h1>')
    artifacts.collect(state, replace(task, stage=Stage.REVIEW), 'owner/repo', publish=False)
    index = artifacts.read(state, task.target, task.issue)
    assert len(index.items) == 2
    assert artifacts.content_path(state, task.target, task.issue, 'prototype').read_text() == '<h1>Revised</h1>'
    # Registering a later artifact must not remove previously registered ones.
    (Path(task.worktree) / '.agent/artifacts.json').write_text('[]')
    artifacts.collect(state, task, 'owner/repo', publish=False)
    assert len(artifacts.read(state, task.target, task.issue).items) == 2


def test_publication_failure_retry_and_pin(tmp_path, monkeypatch):
    state, task = setup(tmp_path)
    calls = []
    def publish(self, artifact, content):
        calls.append(artifact)
        return None if len(calls) == 1 else spec_publish.PublishedReference('https://github.com/owner/repo/blob/task/7/spec.md', 'abc123')
    monkeypatch.setattr(spec_publish.ArtifactPublisher, 'reference', publish)
    from subprocess import CompletedProcess
    monkeypatch.setattr(spec_publish, '_git', lambda *a: CompletedProcess(a, 0, 'abc123\n', ''))
    artifacts.collect(state, task, 'owner/repo')
    assert artifacts.read(state, task.target, task.issue).items[0].publication is None
    artifacts.collect(state, task, 'owner/repo')
    artifacts.pin_published(state, task, 'owner/repo')
    assert '/blob/abc123/spec.md' in artifacts.read(state, task.target, task.issue).items[0].publication.url
    assert len(calls) == 2


def test_retention_terminal_transition_reopen_and_archive(tmp_path):
    state, task = setup(tmp_path)
    save(state, task)
    done = replace(task, stage=Stage.DONE)
    save(state, done)
    done = load(state, task.target, task.issue)
    artifacts.collect(state, done, '', publish=False, now=NOW)
    # Unrelated writes in a terminal state do not restart the clock.
    save(state, replace(done, updated_at=(NOW + timedelta(days=10)).isoformat()))
    assert load(state, task.target, task.issue).terminal_at == NOW.isoformat()
    artifacts.cleanup(state, now=NOW + timedelta(days=29))
    assert artifacts.content_path(state, task.target, task.issue, 'prototype')
    save(state, replace(done, stage=Stage.IMPLEMENT))
    reopened = load(state, task.target, task.issue)
    assert reopened.terminal_at == ''
    artifacts.collect(state, reopened, '', publish=False)
    artifacts.cleanup(state, now=NOW + timedelta(days=31))
    assert artifacts.content_path(state, task.target, task.issue, 'prototype')
    ended = NOW + timedelta(days=40)
    save(state, replace(reopened, stage=Stage.FAILED, updated_at=ended.isoformat()))
    failed = load(state, task.target, task.issue)
    artifacts.collect(state, failed, '', publish=False, now=ended)
    artifacts.cleanup(state, now=ended + timedelta(days=30))
    assert artifacts.content_path(state, task.target, task.issue, 'prototype') is None
    assert artifacts.read(state, task.target, task.issue).expired
    artifacts.collect(state, failed, '', publish=False, now=ended + timedelta(days=31))
    assert artifacts.content_path(state, task.target, task.issue, 'prototype') is None
    assert load_archived(state, task.target, task.issue).stage == Stage.FAILED
    assert Path(task.worktree).exists()  # autopsy worktree is never deleted


def test_paused_pr_open_and_same_issue_on_other_target_never_expire(tmp_path):
    state, task = setup(tmp_path)
    paused = replace(task, stage=Stage.PR_OPEN, park='parked', updated_at='2020-01-01T00:00:00+00:00')
    artifacts.collect(state, paused, '', publish=False)
    artifacts.cleanup(state, now=NOW)
    assert artifacts.content_path(state, task.target, task.issue, 'prototype')
    assert artifacts.read(state, 'beta', task.issue).items == []


def test_rejects_traversal_and_symlink_manifest_and_files(tmp_path):
    state, task = setup(tmp_path)
    secret = tmp_path / 'secret.txt'
    secret.write_text('secret')
    wt = Path(task.worktree)
    (wt / 'link').symlink_to(secret)
    manifest = wt / '.agent/artifacts.json'
    manifest.write_text(json.dumps([
        {'id': 'outside', 'name': 'Outside', 'path': '../secret.txt'},
        {'id': 'symlink', 'name': 'Symlink', 'path': 'link'},
    ]))
    artifacts.collect(state, task, '', publish=False)
    assert [i.id for i in artifacts.read(state, task.target, task.issue).items] == ['spec']
    assert artifacts.content_path(state, task.target, task.issue, '../../secret.txt') is None
    manifest.unlink()
    manifest.symlink_to(secret)
    artifacts.collect(state, task, '', publish=False)
    assert len(artifacts.read(state, task.target, task.issue).items) == 1


def test_routes_survive_teardown_and_flush_with_isolated_html(tmp_path):
    state, task = setup(tmp_path)
    terminal = replace(task, stage=Stage.DONE, terminal_at=NOW.isoformat())
    artifacts.collect(state, terminal, '', publish=False, now=NOW)
    shutil.rmtree(task.worktree)
    fake = FakeSources(state)  # no task state remains after normal done-card flush
    client = TestClient(create_app(make_config(state), fake))
    base = '/api/task/alpha/7'
    assert client.get(base, headers=HEADERS).status_code == 200
    assert client.get(base + '/artifacts').status_code == 401
    body = client.get(base + '/artifacts', headers=HEADERS).json()
    assert len(body['items']) == 2
    url = next(i['url'] for i in body['items'] if i['id'] == 'prototype')
    response = client.get(url, headers=HEADERS)
    assert response.status_code == 200 and '<h1>Prototype</h1>' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'sandbox allow-scripts;' in response.headers['content-security-policy']
    assert 'allow-same-origin' not in response.headers['content-security-policy']
    assert client.get('/api/task/beta/7/artifacts', headers=HEADERS).status_code == 404
    artifacts.cleanup(state, now=NOW + timedelta(days=30))
    assert client.get(url, headers=HEADERS).status_code == 410
    assert client.get(base + '/artifacts', headers=HEADERS).json()['expired']


def test_github_redirect_remains_after_cleanup(tmp_path, monkeypatch):
    state, task = setup(tmp_path)
    monkeypatch.setattr(spec_publish.ArtifactPublisher, 'reference', lambda *a: spec_publish.PublishedReference('https://github.com/o/r/blob/task/7/spec.md', 'abc123'))
    from subprocess import CompletedProcess
    monkeypatch.setattr(spec_publish, '_git', lambda *a: CompletedProcess(a, 0, 'abc123\n', ''))
    done = replace(task, stage=Stage.DONE, terminal_at=NOW.isoformat())
    artifacts.collect(state, done, 'o/r', now=NOW)
    artifacts.pin_published(state, done, 'o/r')
    artifacts.cleanup(state, now=NOW + timedelta(days=30))
    client = TestClient(create_app(make_config(state), FakeSources(state)))
    response = client.get('/api/task/alpha/7/artifacts/spec', headers=HEADERS, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'] == 'https://github.com/o/r/blob/abc123/spec.md'


def test_dispatcher_archives_before_workspace_teardown_and_done_flush(tmp_path, monkeypatch):
    from dispatcher import main
    from tests.webfakes import make_target
    from types import SimpleNamespace
    state, task = setup(tmp_path)
    target = make_target()
    cfg = replace(make_config(state), done_retention_days=0)
    save(state, task)
    monkeypatch.setattr(spec_publish.ArtifactPublisher, 'reference', lambda *a: spec_publish.PublishedReference('https://github.com/o/r/blob/task/7/spec.md', 'abc123'))
    def remove_workspace(*args, **kwargs):
        assert artifacts.content_path(state, task.target, task.issue, 'prototype')
        assert '/blob/abc123/' in artifacts.read(state, task.target, task.issue).items[0].publication.url
        shutil.rmtree(task.worktree)
    monkeypatch.setattr(main, 'remove_workspace', remove_workspace)
    monkeypatch.setattr(main, '_notify', lambda *a, **kw: None)
    deps = SimpleNamespace(
        sessions=SimpleNamespace(end=lambda *a: None),
        github=SimpleNamespace(delete_branch=lambda *a: None),
    )
    main._finish_merged(cfg, deps, target, task)
    main._sync_artifacts(cfg)
    main._flush_done(cfg)
    assert load(state, task.target, task.issue) is None
    assert load_archived(state, task.target, task.issue).stage == Stage.DONE
    assert artifacts.content_path(state, task.target, task.issue, 'prototype')


def test_manifest_backfills_older_prototype_and_questionnaire(tmp_path):
    state, task = setup(tmp_path)
    wt = Path(task.worktree)
    (wt / '.agent/artifacts.json').unlink()
    (wt / '.agent/prototype.html').write_text('<h1>Earlier session</h1>')
    (wt / '.agent/questionnaire.md').write_text('# Questions and answers')
    artifacts.collect(state, task, '', publish=False)
    assert {i.id for i in artifacts.read(state, task.target, task.issue).items} == {'spec', 'prototype', 'questionnaire'}


def test_invalid_manifest_keeps_automatic_spec(tmp_path):
    state, task = setup(tmp_path)
    manifest = Path(task.worktree) / '.agent/artifacts.json'
    for value in ({}, [{'id': '../bad', 'name': 'Bad', 'path': 'preview.html'}],
                  [{'id': 'ok', 'name': '', 'path': 'preview.html'}],
                  [{'id': 'ok', 'name': 'Good', 'path': 42}],
                  [{'id': 'ok', 'name': 'Good', 'path': 'preview.html'}] * 2):
        manifest.write_text(json.dumps(value))
        artifacts.collect(state, task, '', publish=False)
        assert [i.id for i in artifacts.read(state, task.target, task.issue).items] == ['spec']


def test_missing_content_and_unknown_artifact_are_explicit(tmp_path):
    state, task = setup(tmp_path)
    artifacts.collect(state, replace(task, stage=Stage.DONE, terminal_at=NOW.isoformat()), '', publish=False)
    artifacts.content_path(state, task.target, task.issue, 'prototype').unlink()
    client = TestClient(create_app(make_config(state), FakeSources(state)))
    base = '/api/task/alpha/7/artifacts'
    response = client.get(base, headers=HEADERS)
    item = next(i for i in response.json()['items'] if i['id'] == 'prototype')
    assert item['status'] == 'unavailable' and item['url'] == ''
    assert client.get(base + '/prototype', headers=HEADERS).status_code == 404
    assert client.get(base + '/unknown', headers=HEADERS).status_code == 404


def test_legacy_terminal_state_retention_uses_original_completion(tmp_path):
    from dispatcher import state as task_state
    state, task = setup(tmp_path)
    save(state, replace(task, stage=Stage.DONE, done_at=NOW.isoformat()))
    path = state / 'task-alpha-7.json'
    raw = json.loads(path.read_text())
    raw.pop('terminal_at')
    raw['updated_at'] = (NOW + timedelta(days=20)).isoformat()
    path.write_text(json.dumps(raw))
    terminal = task_state.load(state, task.target, task.issue)
    artifacts.collect(state, terminal, '', publish=False)
    artifacts.cleanup(state, now=NOW + timedelta(days=30))
    assert artifacts.read(state, task.target, task.issue).expired


def test_persisted_artifact_schema_survives_collection_and_cleanup(tmp_path):
    from dispatcher.state import archive_root
    state, task = setup(tmp_path)
    artifacts.collect(state, task, '', publish=False)
    path = archive_root(state, task.target, task.issue) / 'index.json'
    raw = json.loads(path.read_text())
    spec = raw['items'][0]
    spec['github_url'] = 'https://github.com/o/r/blob/task/7/spec.md'
    spec['published_commit'] = ''  # Older metadata may lack a verified commit.
    path.write_text(json.dumps(raw))
    artifacts.pin_published(state, task, 'o/r')
    artifacts.collect(state, task, '', publish=False)
    saved = json.loads(path.read_text())
    assert saved == raw
    assert 'publication' not in saved['items'][0]


def test_local_markdown_attachment_and_removal_during_open(tmp_path):
    state, task = setup(tmp_path)
    worktree = Path(task.worktree)
    (worktree / 'data.bin').write_bytes(b'\x00\x01')
    (worktree / '.agent/artifacts.json').write_text(json.dumps([
        {'id': 'data', 'name': 'Data', 'path': 'data.bin'},
    ]))
    done = replace(task, stage=Stage.DONE, terminal_at=NOW.isoformat())
    artifacts.collect(state, done, '', publish=False)
    fake = FakeSources(state)
    client = TestClient(create_app(make_config(state), fake))
    base = '/api/task/alpha/7/artifacts/'
    response = client.get(base + 'spec', headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/plain')
    response = client.get(base + 'data', headers=HEADERS)
    assert response.content == b'\x00\x01'
    assert response.headers['content-disposition'].startswith('attachment;')
    fake.artifact_content = lambda *args: None
    assert client.get(base + 'spec', headers=HEADERS).status_code == 410
