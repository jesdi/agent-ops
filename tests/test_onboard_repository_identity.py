"""Submitted repository spellings retain their settings after GitHub resolution."""
from pathlib import Path

from dispatcher import onboard
from tests.test_onboard import FakeSh, box, gh_repo, run, state


def alias_shell():
    def clone(argv, cwd, env):
        root = Path(argv[-1])
        (root / '.git').mkdir(parents=True)
        (root / 'compose.yaml').write_text('services:\n  postgres: {}\n')
        return ''
    return FakeSh({'gh api repos/jesdi/old-name': gh_repo(202, 'jesdi/new-name'),
                   'gh api repos/jesdi/new-name': gh_repo(202, 'jesdi/new-name'),
                   'git clone': clone, 'remote get-url origin': 'https://github.com/jesdi/new-name.git'})


def submit(tmp_path, gate='my gate', repo='jesdi/old-name', inspect=False):
    onboard.write_intent(state(tmp_path), 'add', 'analytics', {
        'repos': [repo], 'board': {'owner': 'jesdi', 'number': 4},
        'inspect': inspect,
        'commands': [{'setup_cmd': '', 'gate_cmd': gate, 'services': ['postgres']}]},
        actor='agent', epoch_ms=1000)


def test_old_repository_name_keeps_operator_commands_and_services(tmp_path, box):
    submit(tmp_path)
    run(box, alias_shell())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    repo = rec.proposal['repositories'][0]
    assert repo['repo'] == 'jesdi/new-name'
    assert repo['gate_cmd'] == 'my gate'
    assert repo['services'] == ['postgres']
    report = onboard.inspection_view(rec)['repositories'][0]
    assert report['fields']['gate_cmd'] == {'value': 'my gate', 'source': 'you'}
    assert report['selected_services'] == [{'value': 'postgres', 'source': 'you'}]
    assert onboard.can_confirm(rec)


def test_alias_edits_keep_the_same_repository_inspection_request(tmp_path, box):
    from dispatcher import onboarding_sessions
    submit(tmp_path, inspect=True)
    run(box, alias_shell())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    first = onboard.inspection_view(rec)['repositories'][0]['request_id']
    submit(tmp_path, gate='edited gate', inspect=True)
    run(box, alias_shell())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert onboard.inspection_view(rec)['repositories'][0]['request_id'] == first
    assert rec.proposal['repositories'][0]['gate_cmd'] == 'edited gate'
    submit(tmp_path, gate='canonical gate', repo='jesdi/new-name', inspect=True)
    run(box, alias_shell())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert onboard.inspection_view(rec)['repositories'][0]['request_id'] == first
    assert rec.proposal['repositories'][0]['gate_cmd'] == 'canonical gate'
    assert onboarding_sessions.status(state(tmp_path), first)['state'] == 'waiting'


def test_cli_rerun_with_old_name_offers_resolved_command_defaults(tmp_path, box, monkeypatch):
    from dispatcher import onboard_cli
    from tests.test_onboard_cli_rerun import stdin
    submit(tmp_path)
    run(box, alias_shell())
    monkeypatch.setattr('getpass.getuser', lambda: 'agent')
    stdin(monkeypatch, '\n\n\nn\n')
    def kick(*args, **kwargs):
        run(box, alias_shell())
    onboard_cli.main(['project', 'add', 'analytics', 'jesdi/old-name',
                      '--config', str(box)], sh=kick)
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.proposal['repositories'][0]['gate_cmd'] == 'my gate'
    assert rec.proposal['repositories'][0]['services'] == ['postgres']


def test_reordering_aliases_keeps_commands_and_requests_with_repository_ids(tmp_path, box):
    def shell():
        sh = alias_shell()
        sh.route('gh api repos/jesdi/other-old', gh_repo(303, 'jesdi/other-new'))
        sh.route('remote get-url origin', lambda argv, cwd, env:
                 'https://github.com/jesdi/' + Path(argv[2]).name + '.git')
        return sh
    def request(repos, gates):
        onboard.write_intent(state(tmp_path), 'add', 'analytics', {
            'repos': repos, 'board': {'owner': 'jesdi', 'number': 4}, 'inspect': True,
            'commands': [{'gate_cmd': gate} for gate in gates]}, 'agent', 1000)
    request(['jesdi/old-name', 'jesdi/other-old'], ['first gate', 'other gate'])
    run(box, shell())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    before = {x['repo']: x['request_id'] for x in onboard.inspection_view(rec)['repositories']}
    request(['jesdi/other-old', 'jesdi/new-name'], ['other edited', 'first edited'])
    run(box, shell())
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert {x['repo']: x['gate_cmd'] for x in rec.proposal['repositories']} == {
        'jesdi/other-new': 'other edited', 'jesdi/new-name': 'first edited'}
    assert {x['repo']: x['request_id'] for x in onboard.inspection_view(rec)['repositories']} == before


def test_failed_new_resolution_does_not_attach_new_overrides_to_old_report(tmp_path, box):
    from tests.test_onboard import failure
    submit(tmp_path)
    run(box, alias_shell())
    submit(tmp_path, repo='jesdi/missing', gate='unrelated gate')
    sh = alias_shell()
    sh.route('gh api repos/jesdi/missing', failure('404 repository not found'))
    run(box, sh)
    rec = onboard.load_record(state(tmp_path), 'analytics')
    assert rec.step('resolve').state == onboard.StepState.BLOCKED
    assert onboard.inspection_view(rec)['repositories'][0]['fields']['gate_cmd']['value'] != 'unrelated gate'
