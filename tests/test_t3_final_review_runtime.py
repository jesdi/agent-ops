"""Runtime publishes the complete managed launch while retaining raw reproduction."""
import shlex
from dispatcher.runtimes import CODEX, CLAUDE


def test_managed_codex_resume_selects_supervisor_literal_prompt_and_resources():
    plan = CODEX.session_plan('task', '/work tree', 'model', 'high',
        prompt_path='/work tree/.agent/prompt-resume.md', session_id='--last', message='$(false)')
    assert shlex.split(plan.command) == ['python3', '-P', '-m', 'dispatcher.codex_supervisor',
                                         '--model', 'model', '--effort', 'high']
    assert shlex.split(plan.args) == ['--resume=--last', '--prompt-file', '/work tree/.agent/prompt-resume.md']
    assert plan.conversation_id == '--last'
    assert plan.prompt_file == '/work tree/.agent/prompt-resume.md'
    assert plan.support_resources == ('codex-supervisor',)
    assert shlex.split(CODEX.resume_cmd('--last', 'message')) == ['codex', 'resume', '--', '--last', 'message']


def test_managed_claude_fresh_and_resume_select_identity_and_prompt_transport():
    fresh = CLAUDE.session_plan('task', '/work', 'model', '', prompt_path='.agent/prompt.md')
    assert fresh.conversation_id
    assert fresh.args == f'--session-id {fresh.conversation_id} "$(cat .agent/prompt.md)"'
    assert fresh.prompt_file == '.agent/prompt.md'
    assert fresh.support_resources == ()
    resumed = CLAUDE.session_plan('task', '/work', 'model', '', prompt_path='/unused',
                                  session_id='--last', message='A literal $value')
    assert shlex.split(resumed.args) == ['--resume=--last', 'A literal $value']
    assert resumed.conversation_id == '--last'
    assert resumed.prompt_file is None
    assert resumed.support_resources == ()
