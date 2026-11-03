"""Restoring a removed repository starts a fresh inspection through onboarding."""
from pathlib import Path

import pytest

from dispatcher import config, onboard, onboarding_sessions
from tests.test_onboard import FakeSh, box, gh_repo, run, state
from tests.test_onboard_inspection import start


def _repository_shell():
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / 'Makefile').write_text('gate:\n\tpytest\nold:\n\tpytest\n')
        return ''
    return FakeSh({'git clone': clone,
        'gh api repos/jesdi/web': gh_repo(203, 'jesdi/web'),
        'remote get-url origin': lambda argv, cwd, env:
            'https://github.com/jesdi/' + Path(argv[2]).name.removesuffix('.partial') + '.git'})


def _submit(tmp_path, box, repo):
    start(tmp_path, repos=[repo], board={'owner': 'jesdi', 'number': 4})
    run(box, _repository_shell())
    return onboard.load_record(state(tmp_path), 'analytics')


def _request_id(rec):
    return onboard.inspection_view(rec)['repositories'][0]['request_id']


def _draft(command):
    return {'gate_cmd': {'value': command, 'reason': 'Makefile target', 'evidence': 'Makefile'}}


@pytest.mark.parametrize('original_running', [False, True])
def test_restored_repository_gets_a_new_draft_and_ignores_old_completion(
        tmp_path, box, monkeypatch, original_running):
    launched = []
    class Tab:
        def run(self, command):
            launched.append(command)
            return True
    monkeypatch.setattr(onboarding_sessions.herdr.Tab, 'ensure', lambda *args: Tab())
    cfg = config.load_config(box)
    old = _request_id(_submit(tmp_path, box, 'jesdi/analytics'))
    if original_running:
        onboarding_sessions.tick(cfg, None, lambda _: True, tuple, str(box), free=1)
    _submit(tmp_path, box, 'jesdi/web')
    restored = _submit(tmp_path, box, 'jesdi/analytics')

    assert restored.step('inspect').state == onboard.StepState.WAITING
    current = _request_id(restored)
    assert current != old
    assert onboarding_sessions.status(state(tmp_path), old)['state'] == 'withdrawn'
    # Repeating the restored request is idempotent across persisted engine runs.
    assert _request_id(_submit(tmp_path, box, 'jesdi/analytics')) == current

    if original_running:
        assert onboarding_sessions.running(state(tmp_path)) == 1
        assert onboarding_sessions.tick(cfg, None, lambda _: True, tuple, str(box), free=1) == 0
        onboarding_sessions.finish(state(tmp_path), old, result=_draft('make old'))
        run(box, _repository_shell())
        waiting = onboard.load_record(state(tmp_path), 'analytics')
        assert waiting.step('inspect').state == onboard.StepState.WAITING
        assert waiting.proposal['repositories'][0]['gate_cmd'] == ''

    assert onboarding_sessions.tick(cfg, None, lambda _: True, tuple, str(box), free=1) == 1
    onboarding_sessions.finish(state(tmp_path), current, result=_draft('make gate'))
    run(box, _repository_shell())
    ready = onboard.load_record(state(tmp_path), 'analytics')
    assert onboard.can_confirm(ready)
    assert ready.proposal['repositories'][0]['gate_cmd'] == 'make gate'
    assert len(launched) == 1 + int(original_running)
