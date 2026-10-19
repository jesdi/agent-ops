"""Regressions for ticket 11's controller and review boundaries."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dispatcher import artifacts, main, state, task_artifacts
from dispatcher.state import Stage, StageSignal, TaskBundle, TaskRepository
from tests.webfakes import make_task

FRONT, BACK = 'example/front', 'example/back'


@pytest.fixture
def bundle_task(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_OPS_STATE_DIR', str(tmp_path / 'state'))
    front, back = tmp_path / 'front', tmp_path / 'back'
    for root in (front, back):
        (root / '.agent').mkdir(parents=True)
    (front / '.agent/review.html').write_text(artifacts.REVIEW_PAGE_MARKER)
    (front / 'specs/x').mkdir(parents=True)
    (front / 'specs/x/spec.md').write_text('# Origin spec')
    task = make_task(4, target='shop', repo=FRONT, worktree=str(front), spec_path='specs/x/spec.md')
    bundle = TaskBundle(tuple(TaskRepository(repo, str(root), task.branch)
                             for repo, root in ((FRONT, front), (BACK, back))))
    state.save_bundle(tmp_path / 'state', 'shop', 4, bundle, repo=FRONT)
    return tmp_path / 'state', task, back, bundle


def test_secondary_spec_escape_makes_approval_unavailable(bundle_task):
    directory, task, back, bundle = bundle_task
    (back / 'specs').mkdir()
    (back / 'specs/x').symlink_to(Path(task.worktree) / 'specs/x', target_is_directory=True)
    state.save_bundle(directory, 'shop', 4, replace(bundle, spec_repo=BACK), repo=FRONT)
    revision = artifacts.task_plan_revision(task, '.agent/review.html')
    assert not revision.fingerprint
    assert revision.problem


def test_missing_rebound_spec_is_not_offered_as_the_origin_spec(bundle_task):
    directory, task, back, bundle = bundle_task
    task_artifacts.collect(directory, task, FRONT, publish=False)
    assert task_artifacts.read(directory, 'shop', 4, repo=FRONT).items[0].repo == FRONT
    state.select_spec_repo(directory, 'shop', 4, BACK, repo=FRONT, members=[FRONT, BACK])
    task_artifacts.collect(directory, task, FRONT, publish=False)
    assert not task_artifacts.read(directory, 'shop', 4, repo=FRONT).items
    from web.sources import Sources
    assert not Sources(str(directory), None, None).artifacts('shop', 4, FRONT).items
    assert task_artifacts.content_path(directory, 'shop', 4, 'spec', repo=FRONT) is None
    (back / 'specs/x').mkdir(parents=True)
    (back / 'specs/x/spec.md').write_text('# Backend spec')
    task_artifacts.collect(directory, task, FRONT, publish=False)
    assert task_artifacts.read(directory, 'shop', 4, repo=FRONT).items[0].repo == BACK


def test_spec_handover_establishes_validated_exploration_scope(tmp_path, monkeypatch):
    front, back = tmp_path / 'f', tmp_path / 'b'
    front.mkdir()
    task = make_task(4, target='shop', repo=FRONT, worktree=str(front), stage=Stage.SPEC)
    cfg = SimpleNamespace(state_dir=str(tmp_path / 'state'), projects=[SimpleNamespace(
        key='shop', repositories=[SimpleNamespace(repo=r) for r in (FRONT, BACK)])])
    target = SimpleNamespace(name='shop', repo=FRONT, sandbox=True, clone_path='clone')
    calls = []
    monkeypatch.setattr(main, 'create_workspace', lambda *a, **kw: calls.append(kw) or str(front))
    from dispatcher import sandbox_runtime
    monkeypatch.setattr(sandbox_runtime, 'for_clone', lambda clone: SimpleNamespace(
        _plan_for=lambda ref: SimpleNamespace(checkouts={FRONT: str(front), BACK: str(back)})))
    monkeypatch.setattr(main, '_end_session', lambda *a: None)
    monkeypatch.setattr(main, '_spawn_stage', lambda *a: a[3])
    signal = StageSignal('spec', 'done', artifact='specs/x/spec.md', track='normal', repositories=(BACK,))
    turn = SimpleNamespace(cfg=cfg, deps=SimpleNamespace(credentials=None), target=target,
                           signal=signal, dry_run=False)
    main._on_spawn_stage(turn, task, SimpleNamespace(stage=Stage.PLAN, tickets=0), None)
    recorded = state.read_bundle(cfg.state_dir, 'shop', 4, repo=FRONT)
    assert recorded.source == 'exploration'
    assert [r.repo for r in recorded.repositories] == [FRONT, BACK]
    assert [m.repo for m in calls[0]['members']] == [BACK]
    # Later session reports cannot change an established scope.
    turn.signal = replace(signal, repositories=('outside/repo',))
    main._on_spawn_stage(turn, task, SimpleNamespace(stage=Stage.PLAN, tickets=0), None)
    assert len(calls) == 1
    assert state.read_bundle(cfg.state_dir, 'shop', 4, repo=FRONT) == recorded


def test_stage_report_reads_exploration_repository_proposal(tmp_path):
    (tmp_path / '.agent').mkdir()
    (tmp_path / '.agent/stage.json').write_text(json.dumps({
        'stage': 'spec', 'status': 'done', 'repositories': [FRONT, BACK]}))
    assert state.read_stage_signal(tmp_path).repositories == (FRONT, BACK)


@pytest.mark.parametrize('repositories', ['example/back', [None], [123], ['']])
def test_malformed_repository_proposals_are_not_stage_signals(tmp_path, repositories):
    (tmp_path / '.agent').mkdir()
    (tmp_path / '.agent/stage.json').write_text(json.dumps({
        'stage': 'spec', 'status': 'done', 'repositories': repositories}))
    assert state.read_stage_signal(tmp_path) is None


def test_initial_scope_rebuild_preserves_checkout_and_service_data(tmp_path):
    from tests.test_multi_repository_bundle_acceptance import BundleBox
    from tests.test_task_sandbox_acceptance import GIB, compose, make_clone
    from dispatcher.sandbox import EngineError, Limits, SandboxBlocked, SandboxMember, SandboxRequest, TaskSandbox
    box = BundleBox()
    front = make_clone(tmp_path, 'frontend', compose())
    back = make_clone(tmp_path, 'backend', None)
    requests = {'task': SandboxRequest('shop', FRONT, front, services=('postgres',),
                                     branch='task/4', limits=Limits(2 * GIB, 2))}
    sandbox = TaskSandbox(box, tmp_path / 'sandboxes', requests)
    old = sandbox.prepare('task')
    assert sandbox.start('task').ready
    (Path(old.checkout) / 'explored.md').write_text('discovery')
    service = box.container('task', 'postgres')
    box.exec(service, 'sql insert retained')
    requests['task'] = replace(requests['task'], members=(SandboxMember(
        BACK, back, setup='touch setup-back', branch='task/4'),))
    desired = requests['task']
    requests['task'] = replace(desired, agent_demand=Limits(3 * GIB, 2))
    with pytest.raises(SandboxBlocked):
        sandbox.establish_scope('task')
    assert box.plans['task'] == old
    requests['task'] = desired
    plan = sandbox.establish_scope('task')
    assert set(plan.checkouts) == {FRONT, BACK}
    assert (Path(plan.checkout) / 'explored.md').read_text() == 'discovery'
    assert set(old.volumes) <= set(plan.volumes)
    assert sandbox.start('task').ready
    assert (Path(plan.checkouts[BACK]) / 'setup-back').is_file()
    assert box.exec(service, 'sql select').output == 'retained'
    assert sandbox.establish_scope('task') == plan
    # This entry point accepts only an initial origin-only scope.
    requests['task'] = replace(desired, members=(*desired.members, SandboxMember('example/admin', back)))
    with pytest.raises(EngineError, match='origin-only'):
        sandbox.establish_scope('task')


def test_invalid_exploration_scope_parks_before_provisioning(tmp_path, monkeypatch):
    task = make_task(4, target='shop', repo=FRONT, stage=Stage.SPEC)
    cfg = SimpleNamespace(state_dir=str(tmp_path), projects=[SimpleNamespace(
        key='shop', repositories=[SimpleNamespace(repo=FRONT)])])
    turn = SimpleNamespace(cfg=cfg, deps=None, target=SimpleNamespace(sandbox=True),
                           signal=StageSignal('spec', 'done', repositories=('outside/repo',)))
    parks = []
    monkeypatch.setattr(main, '_park_for_input', lambda *a: parks.append(a))
    assert not main._exploration_handover(turn, task, Stage.PLAN)
    assert "not the project's" in parks[0][-1]
    assert state.read_bundle(tmp_path, 'shop', 4, repo=FRONT) is None


@pytest.mark.parametrize('owner', [BACK, 'outside/repo'])
def test_owner_without_declared_scope_is_preserved_and_validated(tmp_path, owner):
    cfg = SimpleNamespace(state_dir=str(tmp_path), projects=[SimpleNamespace(
        key='shop', repositories=[SimpleNamespace(repo=r) for r in (FRONT, BACK)])])
    target = SimpleNamespace(name='shop', repo=FRONT, sandbox=True)
    deps = SimpleNamespace(github=SimpleNamespace(issue_view=lambda *a: {
        'body': f'Spec repository: {owner}'}))
    if owner == BACK:
        assert main._declared_scope(cfg, deps, target, 4, False) == ((), BACK)
    else:
        with pytest.raises(main.ScopeRefused):
            main._declared_scope(cfg, deps, target, 4, False)


def test_secondary_plan_can_be_reviewed_without_origin_artifacts(bundle_task):
    from dispatcher import machine
    directory, task, back, bundle = bundle_task
    state.save_bundle(directory, 'shop', 4, replace(bundle, spec_repo=BACK), repo=FRONT)
    (Path(task.worktree) / '.agent/review.html').unlink()
    (back / '.agent/review.html').write_text(artifacts.REVIEW_PAGE_MARKER)
    (back / '.agent/tickets').mkdir()
    ticket = back / '.agent/tickets/01-one.md'
    ticket.write_text('# One\n\nWhat to build\n\nBlocked by: none\n\n- [ ] Works\n' + 'details ' * 40)
    revision = artifacts.task_plan_revision(task, '.agent/review.html')
    assert revision.fingerprint, revision.problem
    assert machine._checked_plan(task, revision)[0].ok
    assert machine._checked_tickets(task)[0].count == 1
    ticket.write_text(ticket.read_text() + 'changed')
    assert artifacts.task_plan_revision(task, '.agent/review.html').fingerprint != revision.fingerprint


@pytest.mark.parametrize('stage', [Stage.SPEC, Stage.PLAN, Stage.IMPLEMENT])
def test_stage_launch_uses_selected_spec_checkout(tmp_path, monkeypatch, stage):
    from tests.test_main import cfg, deps, make_task as controller_task
    c, d = cfg(tmp_path), deps()
    controller_task(c, issue=42, stage=Stage.QUEUED)
    task = state.load(c.state_dir, c.targets[0].name, 42)
    back = tmp_path / 'backend'
    back.mkdir()
    state.save_bundle(c.state_dir, task.target, 42, TaskBundle((
        TaskRepository(c.targets[0].repo, task.worktree, task.branch),
        TaskRepository(BACK, str(back), task.branch)), spec_repo=BACK), repo=task.repo)
    main._spawn_stage(c, d, c.targets[0], task,
                      main._launch_for(c, c.targets[0], task, stage, lambda m: True, tuple))
    prompt = d.sessions.spawned[-1][3]
    assert f'Spec/plan repository: {BACK}' in prompt
    assert '/workspaces/example~back/.agent/tickets' in prompt
    assert 'push spec files and tickets in /workspaces/example~back' in prompt
    assert f'page under {task.worktree}/.agent' in prompt


def test_exploration_retains_definition_owner_and_hands_over_spec(tmp_path, monkeypatch):
    front, back = tmp_path / 'front', tmp_path / 'back'
    (front / 'specs/x').mkdir(parents=True)
    back.mkdir()
    (front / 'specs/x/spec.md').write_text('# Explored spec')
    cfg = SimpleNamespace(state_dir=str(tmp_path / 'state'), projects=[SimpleNamespace(
        key='shop', repositories=[SimpleNamespace(repo=r) for r in (FRONT, BACK)])])
    target = SimpleNamespace(name='shop', repo=FRONT, sandbox=True, clone_path='clone')
    deps = SimpleNamespace(credentials=None, github=SimpleNamespace(issue_view=lambda *a: {
        'body': f'Spec repository: {BACK}'}))
    monkeypatch.setattr(main, 'create_workspace', lambda *a, **kw: str(front))
    from dispatcher import sandbox_runtime
    monkeypatch.setattr(sandbox_runtime, 'for_clone', lambda clone: SimpleNamespace(
        _plan_for=lambda ref: SimpleNamespace(checkouts={FRONT: str(front), BACK: str(back)})))
    assert main._provision_workspace(cfg, deps, target, SimpleNamespace(number=4), False) == str(front)
    task = make_task(4, target='shop', repo=FRONT, worktree=str(front), stage=Stage.SPEC)
    turn = SimpleNamespace(cfg=cfg, deps=deps, target=target,
                           signal=StageSignal('spec', 'done', artifact='specs/x/spec.md', repositories=(BACK,)))
    main._establish_exploration_scope(turn, task, Stage.PLAN)
    assert state.read_bundle(cfg.state_dir, 'shop', 4, repo=FRONT).spec_repo == BACK
    assert (back / 'specs/x/spec.md').read_text() == '# Explored spec'


def test_secondary_review_page_is_collected_from_its_own_repository(bundle_task):
    directory, task, back, bundle = bundle_task
    state.save_bundle(directory, 'shop', 4, replace(bundle, spec_repo=BACK), repo=FRONT)
    page = artifacts.REVIEW_PAGE_MARKER + '\nBackend plan'
    (back / '.agent/review.html').write_text(page)
    task = replace(task, operator_request=state.PlanApprovalRequest('.agent/review.html', 'revision'))
    task_artifacts.collect(directory, task, FRONT, publish=False)
    item = next(i for i in task_artifacts.read(directory, 'shop', 4, repo=FRONT).items
                if i.path == '.agent/review.html')
    assert item.repo == BACK


def test_exploration_handover_refuses_destination_symlink(tmp_path):
    front, back, outside = tmp_path / 'front', tmp_path / 'back', tmp_path / 'outside'
    (front / 'specs/x').mkdir(parents=True)
    back.mkdir()
    outside.mkdir()
    (front / 'specs/x/spec.md').write_text('# Spec')
    (back / 'specs').symlink_to(outside, target_is_directory=True)
    task = make_task(4, worktree=str(front))
    with pytest.raises(OSError):
        main._handover_spec_files(task, 'specs/x/spec.md', str(back))
    assert not list(outside.iterdir())


def test_resuming_refreshes_selected_repository_context(tmp_path):
    from tests.test_main import cfg, deps, make_task as controller_task
    c, d = cfg(tmp_path), deps()
    controller_task(c, issue=42, stage=Stage.PLAN)
    task = state.load(c.state_dir, c.targets[0].name, 42)
    back = tmp_path / 'backend'
    back.mkdir()
    state.save_bundle(c.state_dir, task.target, 42, TaskBundle((
        TaskRepository(c.targets[0].repo, task.worktree, task.branch),
        TaskRepository(BACK, str(back), task.branch)), spec_repo=BACK), repo=task.repo)
    launch = main._launch_for(c, c.targets[0], task, Stage.PLAN, lambda m: True, tuple)
    main._continue_or_restart(c, d, c.targets[0], task, launch)
    assert f'Spec/plan repository: {BACK}' in d.sessions.resumed[-1][1]
    assert '/workspaces/example~back/.agent/tickets' in d.sessions.resumed[-1][1]


def test_exploration_handover_preserves_destination_files(tmp_path):
    front, back = tmp_path / 'front', tmp_path / 'back'
    for root in (front, back):
        (root / 'specs/x').mkdir(parents=True)
        (root / 'specs/x/spec.md').write_text(root.name)
    task = make_task(4, worktree=str(front))
    main._handover_spec_files(task, 'specs/x/spec.md', str(back))
    assert (back / 'specs/x/spec.md').read_text() == 'back'
    with pytest.raises(main.ScopeRefused):
        main._handover_spec_files(task, '../outside/spec.md', str(back))


def test_definition_owner_before_exploration_defers_spec_publication(tmp_path):
    from tests.test_main import cfg, deps, make_task as controller_task
    c, d = cfg(tmp_path), deps()
    controller_task(c, issue=42, stage=Stage.QUEUED)
    task = state.load(c.state_dir, c.targets[0].name, 42)
    state.save_pending_spec_repo(c.state_dir, task.target, 42, BACK, repo=task.repo)
    main._spawn_stage(c, d, c.targets[0], task,
                      main._launch_for(c, c.targets[0], task, Stage.SPEC, lambda m: True, tuple))
    prompt = d.sessions.spawned[-1][3]
    assert f'definition selects {BACK}' in prompt
    assert 'override the spec stage\'s push requirement' in prompt
    assert 'plan stage commits and pushes it there' in prompt


def test_reprovisioning_preserves_recorded_scope_and_owner(bundle_task, monkeypatch):
    directory, task, back, bundle = bundle_task
    bundle = replace(bundle, spec_repo=BACK)
    state.save_bundle(directory, task.target, task.issue, bundle, repo=FRONT)
    cfg = SimpleNamespace(state_dir=str(directory), projects=[SimpleNamespace(
        key='shop', repositories=[SimpleNamespace(repo=r) for r in (FRONT, BACK)])])
    target = SimpleNamespace(name='shop', repo=FRONT, sandbox=True)
    # An edited issue cannot change a scope that the controller has established.
    def changed_definition(*args):
        pytest.fail('reprovisioning must use the recorded scope, not fetch a new definition')
    deps = SimpleNamespace(credentials=None, github=SimpleNamespace(issue_view=changed_definition))
    calls = []
    monkeypatch.setattr(main, 'create_workspace', lambda *a, **kw: calls.append(kw) or task.worktree)
    assert main._provision_workspace(cfg, deps, target, SimpleNamespace(number=task.issue), False) == task.worktree
    assert [r.repo for r in calls[0]['members']] == [BACK]
    assert state.read_bundle(directory, task.target, task.issue, repo=FRONT) == bundle
