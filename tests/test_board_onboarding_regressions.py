"""Regression coverage for rejected selections, creation recovery and future import."""
import json
import subprocess

import pytest

from tests.test_project_board_onboarding_acceptance import (
    Box, FIELDS, REPO, REF, board, issue, repo_info, run,
)
from tests.test_project_control_board_steps import Boards, control, plan


def test_rejected_board_edit_keeps_preparation_and_import():
    state = board([f for f in FIELDS if f['name'] != 'Impact'])
    box = Box({REPO: repo_info(state, labels=[], issues=[issue(2)])})
    run(box, [REPO], 'edit board=example/404\nyes\n')
    [confirmed] = box.writes
    assert confirmed['board'] == REF
    assert confirmed['board_prep']['fields'] == [dict(name='Impact', type='number')]
    assert confirmed['board_prep']['labels'] == {REPO: ['auto']}
    assert confirmed['backlog']['add'] == {REPO: [2]}


def test_explicitly_unavailable_inspected_board_cannot_be_confirmed():
    box = Box({REPO: {**repo_info(), 'board_state': None}})
    _, _, err = run(box, [REPO], 'yes\nno\n')
    assert not box.writes
    assert 'not accessible' in err


def test_empty_recovery_lookup_never_repeats_uncertain_creation(tmp_path):
    boards = Boards(lose_create=True)
    pc, gh = control(tmp_path, boards)
    op = pc.confirm(plan(dict(owner='example', create=True, title='T')))
    pc.advance(op)
    boards.find = lambda owner, title: []
    pc.advance(op)
    pc.advance(op)
    assert len(boards.boards) == 1
    assert not gh.created
    assert 'unresolved' in pc.status(op)['blocker']


def test_recovery_does_not_adopt_unrelated_board_with_requested_title(tmp_path):
    boards = Boards(lose_create=True)
    pc, gh = control(tmp_path, boards)
    op = pc.confirm(plan(dict(owner='example', create=True, title='T')))
    pc.advance(op)
    boards.boards[:] = [dict(owner='example', title='T', number=99, id='FOREIGN')]
    pc.advance(op)
    assert not boards.applied and not gh.created
    assert pc.status(op)['phase'] == 'blocked'


def test_creation_identity_survives_restart_and_recovers_only_its_board(tmp_path):
    boards = Boards(lose_create=True)
    pc, _ = control(tmp_path, boards)
    op = pc.confirm(plan(dict(owner='example', create=True, title='T')))
    pc.advance(op)
    [created] = boards.boards
    assert created['title'].startswith('T [onboarding ')
    boards.boards.append(dict(owner='example', title='T', number=99, id='FOREIGN'))
    restarted, gh = control(tmp_path, boards)
    restarted.advance(op)
    assert len(boards.boards) == 2
    assert gh.created[0]['projects']['analytics']['board']['id'] == created['id']


def test_ambiguous_creation_recovery_requires_explicit_selection(tmp_path):
    boards = Boards(lose_create=True)
    pc, gh = control(tmp_path, boards)
    op = pc.confirm(plan(dict(owner='example', create=True, title='T')))
    pc.advance(op)
    boards.boards.append({**boards.boards[0], 'number': 99, 'id': 'OTHER'})
    pc.advance(op)
    assert not gh.created and not boards.applied
    assert 'unresolved' in pc.status(op)['blocker']


@pytest.mark.parametrize('fields, total', [([], 0),
    ([dict(name='Status', id='one'), dict(name='Status', id='two')], 2), ([], 1)])
def test_remote_schema_discovery_blocks_incomplete_or_ambiguous_mappings(fields, total):
    from dispatcher.board import BoardError, registered_schema

    def gh(args):
        if args[:2] == ['api', 'graphql']:
            return json.dumps({'data': {'repositoryOwner': {'projectV2': {'id': 'P'}}}})
        return json.dumps({'fields': fields, 'totalCount': total})

    with pytest.raises(BoardError):
        registered_schema('o/1', gh)


def test_future_import_without_committed_board_metadata(tmp_path):
    from dispatcher import triage
    from dispatcher.config import Config, Project, ProjectRepository
    from tests.test_board import SCHEMA, _issue, _node, _page

    nodes, calls, imports = [_node('I1', 1, status='Ready')], [], []

    def gh(args, **kwargs):
        calls.append(args)
        query = next((a for a in args if a.startswith('query=')), '')
        if 'repositoryOwner' in query:
            result = {'data': {'repositoryOwner': {'projectV2': {'id': 'PVT_1'}}}}
        elif args[1:3] == ['project', 'field-list']:
            result = {'fields': [dict(name=name, **field) for name, field in SCHEMA['fields'].items()]}
            for field in result['fields']:
                if 'options' in field:
                    field['options'] = [dict(name=n, id=i) for n, i in field['options'].items()]
        elif 'addProjectV2ItemById' in query:
            imports.append(next(a for a in args if a.startswith('c=')))
            nodes.append(_node('I2', 2))
            result = {}
        elif 'items(first:' in query:
            return subprocess.CompletedProcess(args, 0, _page(nodes), '')
        elif args[1] == 'api' and '--paginate' in args:
            result = [[_issue(1), _issue(2)]]
        elif args[1:3] == ['issue', 'view']:
            result = {'id': 'ISSUE2'}
        else:
            assert args[1:3] == ['issue', 'list']
            result = [_issue(1), _issue(2)]
        return subprocess.CompletedProcess(args, 0, json.dumps(result), '')

    member = ProjectRepository('o/a', str(tmp_path), '/wt', 'rank', 'gate')
    project = Project('p', 'p', 'o', 1, 'F_STATUS', 'o_ready', 'o_prog', [member])
    cfg = Config(str(tmp_path / 'state'), 2, '1500m', '2', [], projects=[project])
    for _ in range(2):
        brd = triage._board(cfg, 'o/a', [], gh)
        assert brd is not None, 'configured board must work without clone metadata'
        assert brd.items[1].status == 'Ready'
    assert imports == ['c=ISSUE2']
    assert calls
