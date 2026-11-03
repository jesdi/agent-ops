"""Repository inspection through the onboarding unit's durable intent interface."""
import json
from pathlib import Path

import pytest

from dispatcher import onboard
from tests.test_onboard import FakeSh, box, run, state


def start(tmp_path, **changes):
    request = {"repos": ["jesdi/analytics"], "board": None,
               "commands": [{"gate_cmd": None}], "inspect": True, **changes}
    onboard.write_intent(state(tmp_path), "add", "analytics", request, "agent", 1000)


def test_start_without_board_or_commands_reads_repository_facts(tmp_path, box):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / '.backlog').mkdir()
        (root / '.backlog/project-meta.json').write_text(json.dumps({"owner": "jesdi", "projectNumber": 4}))
        (root / 'compose.yaml').write_text('services:\n  postgres:\n    image: postgres\n')
        (root / '.env.example').write_text('API_TOKEN=do-not-copy-this-value\n')
        (root / 'Makefile').write_text('gate:\n\tpytest\n')
        return ''
    start(tmp_path)
    run(box, FakeSh({'git clone': clone}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.step('inspect').state == onboard.StepState.WAITING
    assert rec.proposal['board'] == {'owner': 'jesdi', 'number': 4}
    report = onboard.inspection_view(rec)['repositories'][0]
    assert report['services'] == [{'value': 'postgres', 'source': 'rule: compose.yaml'}]
    assert report['secrets'] == [{'value': 'API_TOKEN', 'source': 'rule: .env.example'}]
    assert 'do-not-copy-this-value' not in json.dumps(onboard.inspection_view(rec))
    assert not onboard.can_confirm(rec)


def test_skip_then_supply_gate_and_board_reaches_confirm(tmp_path, box):
    start(tmp_path)
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    onboard.write_intent(state(tmp_path), 'skip-inspection', 'analytics',
                         {'revision': rec.revision}, 'agent', 1001)
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.step('inspect').state == onboard.StepState.VERIFIED
    assert not onboard.can_confirm(rec)
    start(tmp_path, board={'owner': 'jesdi', 'number': 4}, commands=[{'gate_cmd': 'make gate'}])
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert onboard.can_confirm(rec)
    assert rec.awaiting == 'confirm'
    assert onboard.inspection_view(rec)['repositories'][0]['fields']['gate_cmd'] == {'value': 'make gate', 'source': 'you'}


def finish(tmp_path, box, rec, monkeypatch, result=None, error=''):
    from dispatcher import config, onboarding_sessions
    class Tab:
        def run(self, command):
            return True
    monkeypatch.setattr(onboarding_sessions.herdr.Tab, 'ensure', lambda *args: Tab())
    onboarding_sessions.tick(config.load_config(box), None, lambda _: True,
                             lambda xs: xs, str(box), free=1)
    request_id = onboard.inspection_view(rec)['repositories'][0]['request_id']
    onboarding_sessions.finish(state(tmp_path), request_id, result=result, error=error)


def test_inspection_reports_declared_skill_names(tmp_path, box):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / '.my-skills.json').write_text(json.dumps({'schemaVersion': 1, 'skills': {'tdd': {'version': '1'}}}))
        return ''
    start(tmp_path)
    run(box, FakeSh({'git clone': clone}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert onboard.inspection_view(rec)['repositories'][0]['skills'] == ['tdd']


def test_cli_can_start_inspection_without_board_or_command_prompts(tmp_path, box, monkeypatch):
    from dispatcher import onboard_cli
    monkeypatch.setattr('getpass.getuser', lambda: 'agent')
    monkeypatch.setattr('builtins.input', lambda *_: (_ for _ in ()).throw(AssertionError('unexpected prompt')))
    def kick(*args, **kwargs):
        run(box, FakeSh())
    assert onboard_cli.main(['project', 'add', 'analytics', 'jesdi/analytics', '--config', str(box)], sh=kick) == 0
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.step('inspect').state == onboard.StepState.WAITING


def test_draft_commands_and_services_reach_confirm_and_config(tmp_path, box, monkeypatch):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / 'Makefile').write_text('gate:\n\tpytest\n')
        (root / 'compose.yaml').write_text('services:\n  postgres: {}\n')
        return ''
    start(tmp_path, board={'owner': 'jesdi', 'number': 4})
    run(box, FakeSh({'git clone': clone}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    finish(tmp_path, box, rec, monkeypatch, {
        'gate_cmd': {'value': 'make gate', 'reason': 'Makefile target gate', 'evidence': 'Makefile'},
        'services': [{'value': 'postgres', 'reason': 'tests use postgres'}]})
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.awaiting == 'confirm' and onboard.can_confirm(rec)
    assert rec.proposal['repositories'][0]['gate_cmd'] == 'make gate'
    assert rec.proposal['repositories'][0]['services'] == ['postgres']
    from dispatcher.infra_config import project_block
    assert 'postgres' in project_block(rec.proposal)
    assert onboard.inspection_view(rec)['repositories'][0]['fields']['gate_cmd']['source'] == 'draft: Makefile target gate'


def test_invalid_draft_warns_and_does_not_guess_a_gate(tmp_path, box, monkeypatch):
    start(tmp_path, board={'owner': 'jesdi', 'number': 4})
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    finish(tmp_path, box, rec, monkeypatch, {'gate_cmd': {'value': 'make imaginary', 'reason': 'guess', 'evidence': 'Makefile'}})
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.step('inspect').state == onboard.StepState.VERIFIED
    assert 'draft unavailable:' in rec.step('inspect').detail
    assert not onboard.can_confirm(rec)


def test_conflicting_repository_boards_require_operator_choice(tmp_path, box):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / '.backlog').mkdir()
        number = 4 if root.name.startswith('analytics') else 5
        (root / '.backlog/project-meta.json').write_text(json.dumps({'owner': 'jesdi', 'projectNumber': number}))
        return ''
    start(tmp_path, repos=['jesdi/analytics', 'jesdi/web'], commands=[{}, {}])
    from tests.test_onboard import gh_repo
    run(box, FakeSh({'git clone': clone, 'gh api repos/jesdi/web': gh_repo(203, 'jesdi/web'), 'remote get-url origin': lambda argv, cwd, env: 'https://github.com/jesdi/' + Path(argv[2]).name.removesuffix('.partial') + '.git'}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert len(onboard.inspection_view(rec)['repositories']) == 2, rec.steps
    assert rec.proposal['board'] is None


def test_late_draft_keeps_operator_commands_including_explicit_empty(tmp_path, box, monkeypatch):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / 'Makefile').write_text('gate:\n\tpytest\nsetup:\n\tpip install .\n')
        return ''
    start(tmp_path, board={'owner': 'jesdi', 'number': 4})
    run(box, FakeSh({'git clone': clone}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    start(tmp_path, board={'owner': 'jesdi', 'number': 4}, commands=[{'gate_cmd': 'custom gate', 'setup_cmd': ''}])
    run(box, FakeSh())
    finish(tmp_path, box, rec, monkeypatch, {
        'gate_cmd': {'value': 'make gate', 'reason': 'gate target', 'evidence': 'Makefile'},
        'setup_cmd': {'value': 'make setup', 'reason': 'setup target', 'evidence': 'Makefile'}})
    run(box, FakeSh())
    current = onboard.load_record(state(tmp_path), 'analytics')
    assert current.proposal['repositories'][0]['gate_cmd'] == 'custom gate'
    assert current.proposal['repositories'][0]['setup_cmd'] == ''
    assert onboard.inspection_view(current)['repositories'][0]['fields']['setup_cmd']['source'] == 'you'


def test_failed_draft_is_a_warning_and_manual_input_remains(tmp_path, box, monkeypatch):
    start(tmp_path, board={'owner': 'jesdi', 'number': 4}, commands=[{'gate_cmd': 'make gate'}])
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    finish(tmp_path, box, rec, monkeypatch, error='model unavailable')
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.step('inspect').state == onboard.StepState.VERIFIED
    assert rec.step('inspect').detail == 'draft unavailable: model unavailable'
    assert onboard.can_confirm(rec)


def test_operator_cannot_confirm_a_service_not_found_in_compose(tmp_path, box):
    start(tmp_path, board={'owner': 'jesdi', 'number': 4},
          commands=[{'gate_cmd': 'make gate', 'services': ['invented']}])
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    onboard.write_intent(state(tmp_path), 'skip-inspection', 'analytics', {'revision': rec.revision}, 'agent', 1001)
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert not onboard.can_confirm(rec)
    assert 'services not found in Compose files: invented' in rec.step('confirm').detail


def test_removed_repository_request_is_withdrawn(tmp_path, box):
    from dispatcher import onboarding_sessions
    from tests.test_onboard import gh_repo
    start(tmp_path)
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    old = onboard.inspection_view(rec)['repositories'][0]['request_id']
    start(tmp_path, repos=['jesdi/web'])
    run(box, FakeSh({'gh api repos/jesdi/web': gh_repo(203, 'jesdi/web'),
                     'remote get-url origin': 'https://github.com/jesdi/web.git'}))
    assert onboarding_sessions.status(state(tmp_path), old)['state'] == 'withdrawn'
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert [x['repo'] for x in onboard.inspection_view(rec)['repositories']] == ['jesdi/web']


def test_nonexistent_target_is_rejected_even_when_makefile_exists(tmp_path, box, monkeypatch):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / 'Makefile').write_text('real:\n\tpytest\n')
        return ''
    start(tmp_path)
    run(box, FakeSh({'git clone': clone}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    finish(tmp_path, box, rec, monkeypatch, {'gate_cmd': {'value': 'make imaginary', 'reason': 'guess', 'evidence': 'Makefile'}})
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.proposal['repositories'][0]['gate_cmd'] == ''
    assert rec.step('inspect').detail.startswith('draft unavailable:')


def draft_for_repository(tmp_path, box, monkeypatch, files, draft):
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        for name, contents in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        return ''
    start(tmp_path, board={'owner': 'jesdi', 'number': 4})
    run(box, FakeSh({'git clone': clone}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    finish(tmp_path, box, rec, monkeypatch, draft)
    run(box, FakeSh())
    return onboard.load_record(state(tmp_path), 'analytics')


@pytest.mark.parametrize(('command', 'scripts', 'expected'), [
    ('pnpm run gate', {'gate': 'vitest run'}, 'pnpm run gate'),
    ('npm ci', {}, 'npm ci'),
    ('npm run absent', {'gate': 'vitest run'}, ''),
    ('npm run gate', ['gate'], ''),
    ('python -m pytest', {'gate': 'vitest run'}, ''),
])
def test_draft_package_commands_require_install_or_a_declared_script(
        tmp_path, box, monkeypatch, command, scripts, expected):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'package.json': json.dumps({'scripts': scripts})},
        {'gate_cmd': {'value': command, 'reason': 'package script', 'evidence': 'package.json'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == expected


@pytest.mark.parametrize(('command', 'evidence', 'expected'), [
    ('python scripts/check.py', 'scripts/check.py', 'python scripts/check.py'),
    ('pytest', 'pyproject.toml', ''),
    ('make gate', '../outside', ''),
    ('make gate', '/tmp/outside', ''),
    ('make gate', '', ''),
])
def test_draft_command_requires_readable_repository_evidence(
        tmp_path, box, monkeypatch, command, evidence, expected):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'scripts/check.py': 'print("checked")', 'pyproject.toml': '[project]\nname="demo"'},
        {'gate_cmd': {'value': command, 'reason': 'repository evidence', 'evidence': evidence}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == expected


@pytest.mark.parametrize('entry', [None, {'value': 3, 'reason': 'declared'},
    {'value': 'TOKEN', 'reason': None}, {'value': 'TOKEN', 'reason': '  '},
    {'value': 'not-a-secret-name', 'reason': 'declared'}])
def test_invalid_draft_secret_names_do_not_replace_rule_facts(tmp_path, box, monkeypatch, entry):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'.env.example': 'EXISTING_TOKEN=example\n'}, {'secrets': [entry]})
    assert onboard.inspection_view(rec)['repositories'][0]['secrets'] == [
        {'value': 'EXISTING_TOKEN', 'source': 'rule: .env.example'}]


def test_valid_draft_secret_name_adds_its_reason(tmp_path, box, monkeypatch):
    rec = draft_for_repository(tmp_path, box, monkeypatch, {},
        {'secrets': [{'value': 'API_TOKEN', 'reason': 'client configuration'}]})
    assert onboard.inspection_view(rec)['repositories'][0]['secrets'] == [
        {'value': 'API_TOKEN', 'source': 'draft: client configuration'}]


def test_draft_cannot_select_a_service_missing_from_compose(tmp_path, box, monkeypatch):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'compose.yaml': 'services:\n  postgres: {}\n'},
        {'services': [{'value': 'redis', 'reason': 'guessed cache'}]})
    assert rec.proposal['repositories'][0]['services'] == []
    assert onboard.inspection_view(rec)['repositories'][0]['services'] == [
        {'value': 'postgres', 'source': 'rule: compose.yaml'}]


def test_first_manual_cli_reads_facts_without_requesting_a_draft(tmp_path, box, monkeypatch):
    from dispatcher import onboard_cli, onboarding_sessions
    from tests.test_onboard_cli_rerun import stdin
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / '.env.example').write_text('API_TOKEN=example\n')
        (root / 'compose.yaml').write_text('services:\n  postgres: {}\n')
        return ''
    monkeypatch.setattr('getpass.getuser', lambda: 'agent')
    stdin(monkeypatch, '\nmake gate\n\nn\n')
    def kick(*args, **kwargs):
        run(box, FakeSh({'git clone': clone}))
    onboard_cli.main(['project', 'add', 'analytics', 'jesdi/analytics', '--manual',
                      '--board', 'jesdi/4', '--config', str(box)], sh=kick)
    rec = onboard.load_record(state(tmp_path), 'analytics')
    report = onboard.inspection_view(rec)['repositories'][0]
    assert report['state'] == 'skipped'
    assert report['secrets'] == [{'value': 'API_TOKEN', 'source': 'rule: .env.example'}]
    assert report['services'] == [{'value': 'postgres', 'source': 'rule: compose.yaml'}]
    assert not list((state(tmp_path) / onboarding_sessions.DIRECTORY).glob('*/request.json'))


@pytest.mark.parametrize(('command', 'evidence'), [
    ('make -f Makefile imaginary', 'Makefile'),
    ('python check.py', 'scripts/check.py'),
    ('python scripts/check.py.backup', 'scripts/check.py'),
])
def test_evidence_filename_does_not_bypass_command_validation(tmp_path, box, monkeypatch, command, evidence):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'Makefile': 'gate:\n\tpytest\n', 'scripts/check.py': 'print("ok")'},
        {'gate_cmd': {'value': command, 'reason': 'file exists', 'evidence': evidence}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == ''


def test_explicit_makefile_keeps_valid_target(tmp_path, box, monkeypatch):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'Makefile': 'gate:\n\tpytest\n'},
        {'gate_cmd': {'value': 'make -f Makefile gate', 'reason': 'gate target', 'evidence': 'Makefile'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == 'make -f Makefile gate'


def test_edit_to_manual_withdraws_waiting_draft_but_keeps_facts(tmp_path, box):
    from dispatcher import onboarding_sessions
    start(tmp_path, board={'owner': 'jesdi', 'number': 4})
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    ident = onboard.inspection_view(rec)['repositories'][0]['request_id']
    start(tmp_path, board={'owner': 'jesdi', 'number': 4}, inspect=False,
          commands=[{'gate_cmd': 'make gate'}])
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert onboard.inspection_view(rec)['repositories'][0]['state'] == 'skipped'
    assert onboarding_sessions.status(state(tmp_path), ident)['state'] == 'withdrawn'
    assert onboard.can_confirm(rec)


def test_confirmation_readiness_does_not_come_from_error_text(tmp_path, box):
    from tests.test_onboard import failure
    start(tmp_path, board={'owner': 'jesdi', 'number': 4}, inspect=False,
          commands=[{'gate_cmd': 'make gate'}])
    run(box, FakeSh({'gh api graphql': failure('review the proposal, remote lookup failed')}))
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.step('confirm').state == onboard.StepState.FAILED
    assert rec.step('confirm').detail.startswith('review the proposal,')
    assert not onboard.can_confirm(rec)


def test_preinspection_record_recovers_confirmation_readiness_on_unit_pass(tmp_path, box):
    from tests.test_onboard import add
    add(tmp_path)
    run(box, FakeSh())
    path = state(tmp_path) / 'onboarding/analytics.json'
    old = json.loads(path.read_text())
    old.pop('confirm_ready')
    old.pop('inspection')
    old['steps'] = [x for x in old['steps'] if x['name'] != 'inspect']
    path.write_text(json.dumps(old))
    run(box, FakeSh())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert onboard.can_confirm(rec)
    assert onboard.inspection_view(rec) == {'repositories': []}
