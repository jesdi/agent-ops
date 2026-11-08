"""Operator launch boundary: the prompt, role config and runtime share one grant."""
import json

import pytest

from dispatcher import main, containers, implementation_grants
from dispatcher.models import Entry
from dispatcher.state import Stage, load
from tests.test_main import deps, make_task
from tests.test_review_second_acceptance import GrantSessions, _cfg, _verdict


@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('granted', [False, True])
def test_implement_launch_bounds_driver_roles_to_current_grants(tmp_path, monkeypatch, codex_package, resume, granted):
    c = _cfg(tmp_path)
    wt = make_task(c, spec_path='specs/approved/spec.md')
    task = load(c.state_dir, 'portfolio_eval', 42)
    primary = Entry('anthropic', 'claude-opus-5', 'high')
    launch = main._with_second(c, c.repositories[0], main.Launch(Stage.IMPLEMENT, primary),
                               _verdict(granted))
    sess = GrantSessions()
    (wt / '.agent' / 'ledger.md').write_text('ticket 1 merged\n')
    (wt / '.agent' / 'runtime-models.json').write_text('{"stale": ["openai/denied@high"]}')
    if resume:
        main._continue_or_restart(c, deps(sess=sess), c.repositories[0], task, launch,
                                  message='Retry the approved failing ticket.', gh_config='')
        prompt = sess.resumed[0][1]
    else:
        main._launch_stage(c, deps(sess=sess), c.repositories[0], task, launch,
                           message='Retry the approved failing ticket.', gh_config='')
        prompt = sess.spawned[0][3]
    runtime_grant = sess.resume_seconds[0] if resume else sess.spawn_seconds[0][1]
    (wt / '.git').write_text(f'gitdir: {tmp_path}/clone/.git/worktrees/task-42\n')
    monkeypatch.setenv('AGENT_OPS_STATE_DIR', str(tmp_path / 'state'))
    cmd = containers.session_cmd('task-42', str(wt), '2g', '2', primary.model_id, 'prompt',
                                 second=runtime_grant)
    assert ('/opt/codex' in cmd) == granted
    assert ('/root/.codex' in cmd) == granted
    assert '--models .agent/runtime-models.json' in prompt
    assert 'Retry the approved failing ticket.' in prompt
    models = json.loads((wt / '.agent' / 'runtime-models.json').read_text())
    assert set(models) == {'test-writer', 'implementer', 'reviewer',
                           'fixer', 'final-reviewer', 'conflict', 'pr-writer'}
    assert models['implementer'] == [str(primary), str(primary)]
    expected_review = 'openai/gpt-5-codex@medium' if granted else str(primary)
    assert models['reviewer'] == models['final-reviewer'] == [expected_review]
    assert set(sum(models.values(), [])) == {str(primary), expected_review}
    assert 'preflight' in prompt and 'ledger' in prompt and 'PR' in prompt
    assert ('same-provider' in prompt) == (not granted)
    assert (wt / '.agent' / 'ledger.md').read_text() == 'ticket 1 merged\n'
    assert load(c.state_dir, 'portfolio_eval', 42).spec_path == task.spec_path


def test_primary_codex_is_the_only_grant_and_gets_valid_default_effort(tmp_path):
    primary = Entry('openai', 'gpt-6-astra')
    text = implementation_grants.prepare(str(tmp_path), 'implement', primary, None, 'Retry.')
    models = json.loads((tmp_path / '.agent/runtime-models.json').read_text())
    assert set(sum(models.values(), [])) == {'openai/gpt-6-astra@medium'}
    assert text.startswith('Retry.') and 'same-provider' in text


def test_other_stages_do_not_rewrite_implementation_inputs(tmp_path):
    primary = Entry('anthropic', 'claude-opus-5', 'high')
    assert implementation_grants.prepare(str(tmp_path), 'review', primary, None, 'Review.') == 'Review.'
    assert not (tmp_path / '.agent').exists()
