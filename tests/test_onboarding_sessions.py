"""Onboarding session requests share dispatcher capacity without task records."""
from dispatcher import onboarding_sessions as sessions
from dispatcher.config import Config


def cfg(tmp_path):
    return Config(str(tmp_path), 2, '1g', '1', [])


def request(tmp_path, project='demo', revision='one'):
    clone = tmp_path / 'clone'
    clone.mkdir(parents=True, exist_ok=True)
    return sessions.ensure_request(tmp_path, project, 'owner/repo', revision, str(clone), {})


def test_dispatcher_full_keeps_request_waiting_without_expiry(tmp_path):
    ident = request(tmp_path)
    assert sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=0) == 0
    assert sessions.status(tmp_path, ident) == {
        'state': 'waiting', 'detail': 'waiting for a free execution unit (box full: 2/2)', 'result': None}
    assert request(tmp_path) == ident
    assert sessions.running(tmp_path) == 0


def test_dispatcher_usage_wait_then_launch_reserves_once(tmp_path, monkeypatch):
    ident = request(tmp_path)
    sessions.tick(cfg(tmp_path), None, lambda _: False, lambda xs: xs, 'targets.yaml', free=2)
    assert sessions.status(tmp_path, ident)['state'] == 'waiting'
    assert 'usage' in sessions.status(tmp_path, ident)['detail']
    launches = []
    class Tab:
        def run(self, command):
            launches.append(command)
            return True
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    assert sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=2) == 1
    assert sessions.running(tmp_path) == 1
    assert sessions.status(tmp_path, ident)['state'] == 'running'
    assert sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=1) == 0
    assert len(launches) == 1


def test_withdrawing_running_request_keeps_capacity_until_runner_finishes(tmp_path, monkeypatch):
    ident = request(tmp_path)
    class Tab:
        def run(self, command):
            return True
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=2)
    sessions.withdraw(tmp_path, ident)
    assert sessions.status(tmp_path, ident)['state'] == 'withdrawn'
    assert sessions.running(tmp_path) == 1
    sessions.finish(tmp_path, ident, result={'gate_cmd': 'make gate'})
    assert sessions.running(tmp_path) == 0
    assert sessions.status(tmp_path, ident)['state'] == 'withdrawn'
    assert sessions.status(tmp_path, ident)['result'] is None


def test_inspection_container_only_mounts_model_auth_and_read_only_inputs(tmp_path, monkeypatch):
    from dispatcher import containers
    monkeypatch.setattr(containers, '_host_binary', lambda runtime: ['-v', '/cli:/usr/local/bin/' + runtime.cli + ':ro'])
    for model in ('anthropic/claude-sonnet-4-5', 'openai/gpt-5'):
        argv = containers.inspection_cmd('inspect-a', '/clone', '/request', '/state', '1g', '1', model)
        mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == '-v']
        assert '/clone:/repository:ro,noexec,nosuid,nodev' in mounts
        assert '/request/input:/inspection:ro' in mounts
        assert all('gh' not in mount and '.gitconfig' not in mount for mount in mounts)
        assert all('/state:' not in mount for mount in mounts)
        assert argv[argv.index('-w') + 1] == '/inspection'
        assert '--dangerously-bypass-approvals-and-sandbox' not in argv
        if model.startswith('anthropic'):
            assert argv[argv.index('--tools') + 1] == ''
            assert '--safe-mode' in argv
            assert '--restricted' in argv
        else:
            assert 'shell_tool' in argv
            assert 'unified_exec' in argv
            assert 'hooks' in argv
            assert argv[argv.index('--sandbox') + 1] == 'read-only'


def test_dispatcher_runs_inspection_before_new_claims_and_counts_it_next_pass(tmp_path, monkeypatch):
    from dataclasses import replace
    from dispatcher import main
    from dispatcher.github import Candidate
    from tests.test_main import cfg as task_cfg, deps, FakeGitHub, FakeSessions, patch_usage, patch_workspace
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    configuration = replace(task_cfg(tmp_path), capacity=1)
    ident = request(__import__('pathlib').Path(configuration.state_dir))
    launches = []
    class Tab:
        def run(self, command):
            launches.append(command)
            return True
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    github = FakeGitHub([Candidate(42, 'New task', 'url')])
    for _ in range(2):
        main.run_pass(configuration, deps(github, FakeSessions()))
    assert len(launches) == 1
    assert github.claimed == []
    assert sessions.status(configuration.state_dir, ident)['state'] == 'running'


def test_dispatcher_recovers_interrupted_launch_without_duplicate(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from subprocess import CompletedProcess
    ident = request(tmp_path)
    class Tab:
        def run(self, command):
            return False  # acknowledgement lost: reserve, never launch twice
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    monkeypatch.setattr(sessions.herdr.Tab, 'find', lambda *a: None)
    monkeypatch.setattr(sessions.herdr, 'tab_labels', lambda: set())
    sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=2)
    assert sessions.running(tmp_path) == 1
    checks = []
    def run(argv, **kw):
        checks.append(argv)
        return CompletedProcess(argv, 1, '', '')  # podman confirms container absent
    sessions.reconcile(tmp_path, run=run, now=datetime.now(timezone.utc) + timedelta(minutes=20))
    assert sessions.running(tmp_path) == 0
    assert sessions.status(tmp_path, ident)['state'] == 'failed'
    assert 'interrupted' in sessions.status(tmp_path, ident)['detail']
    assert checks[0][:3] == ['podman', 'container', 'exists']


def test_unknown_container_state_never_frees_reserved_capacity(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from subprocess import CompletedProcess
    ident = request(tmp_path)
    class Tab:
        def run(self, command):
            return True
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=2)
    monkeypatch.setattr(sessions.herdr.Tab, 'find', lambda *a: None)
    monkeypatch.setattr(sessions.herdr, 'tab_labels', lambda: set())
    sessions.reconcile(tmp_path, run=lambda argv, **kw: CompletedProcess(argv, 125),
                       now=datetime.now(timezone.utc) + timedelta(minutes=20))
    assert sessions.running(tmp_path) == 1
    assert sessions.status(tmp_path, ident)['state'] == 'running'


def test_runner_only_passes_inert_manifest_text_and_records_result(tmp_path, monkeypatch):
    import json
    from subprocess import CompletedProcess
    from dispatcher import containers, onboarding_runner
    ident = request(tmp_path)
    clone = tmp_path / 'clone'
    (clone / 'Makefile').write_text('gate:\n\techo should-never-run\n')
    (clone / '.env').write_text('PASSWORD=do-not-send')
    outside = tmp_path / 'private'
    outside.write_text('private contents')
    (clone / 'package.json').symlink_to(outside)
    class Tab:
        def run(self, command):
            return True
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    monkeypatch.setattr(containers, '_host_binary', lambda runtime: [])
    sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=1)
    draft = {'gate_cmd': {'value': 'make gate', 'reason': 'Makefile target gate', 'evidence': 'Makefile'}}
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        assert 'should-never-run' in kw['input']  # text only, no command invocation
        assert 'do-not-send' not in kw['input']
        assert 'private contents' not in kw['input']
        kw['stdout'].write(json.dumps(draft).encode())
        return CompletedProcess(argv, 0)
    onboarding_runner.run_request(tmp_path, ident, '1g', '1', 'anthropic/model', run=run)
    assert len(calls) == 1
    assert calls[0][:2] == ['podman', 'run']
    assert sessions.status(tmp_path, ident) == {'state': 'complete', 'detail': '', 'result': draft}
    assert sessions.running(tmp_path) == 0


def test_runner_bad_json_is_a_failed_draft_and_frees_capacity(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from dispatcher import containers, onboarding_runner
    ident = request(tmp_path)
    class Tab:
        def run(self, command):
            return True
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    monkeypatch.setattr(containers, '_host_binary', lambda runtime: [])
    sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=1)
    def run(argv, **kw):
        kw['stdout'].write(b'not json')
        return CompletedProcess(argv, 0)
    onboarding_runner.run_request(tmp_path, ident, '1g', '1', 'anthropic/model', run=run)
    assert sessions.status(tmp_path, ident)['state'] == 'failed'
    assert sessions.running(tmp_path) == 0


def test_woken_task_has_priority_over_inspection(tmp_path, monkeypatch):
    from dataclasses import replace
    from pathlib import Path
    from dispatcher import main
    from dispatcher.state import PARK_WAKE, Stage, SessionRecord, write_session
    from tests.test_main import cfg as task_cfg, deps, FakeSessions, make_task, patch_usage, patch_workspace
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    configuration = replace(task_cfg(tmp_path), capacity=1)
    make_task(configuration, issue=42, stage=Stage.IMPLEMENT, park=PARK_WAKE)
    write_session(configuration.state_dir, 'portfolio_eval', 42, SessionRecord('resume-42', 'implement'))
    ident = request(Path(configuration.state_dir))
    live = FakeSessions()
    main.run_pass(configuration, deps(sess=live))
    assert ('portfolio_eval', 42) in live.resume_calls
    assert sessions.status(configuration.state_dir, ident)['state'] == 'waiting'
    assert 'box full' in sessions.status(configuration.state_dir, ident)['detail']


def test_runner_timeout_kills_container_before_releasing_capacity(tmp_path, monkeypatch):
    from subprocess import CompletedProcess, TimeoutExpired
    from dispatcher import containers, onboarding_runner
    ident = request(tmp_path)
    class Tab:
        def run(self, command):
            return True
    monkeypatch.setattr(sessions.herdr.Tab, 'ensure', lambda *a, **kw: Tab())
    monkeypatch.setattr(containers, '_host_binary', lambda runtime: [])
    sessions.tick(cfg(tmp_path), None, lambda _: True, lambda xs: xs, 'targets.yaml', free=1)
    calls = []
    def run(argv, **kw):
        calls.append(argv[:2])
        if argv[:2] == ['podman', 'run']:
            raise TimeoutExpired(argv, 600)
        assert sessions.running(tmp_path) == 1
        return CompletedProcess(argv, 0)
    onboarding_runner.run_request(tmp_path, ident, '1g', '1', 'anthropic/model', run=run)
    assert calls == [['podman', 'run'], ['podman', 'kill']]
    assert sessions.running(tmp_path) == 0
    assert sessions.status(tmp_path, ident)['detail'] == 'inspection timed out after 10 minutes'
