"""Command drafts are validated through the operator-visible onboarding proposal."""
import json

import pytest

from tests.test_onboard import box
from tests.test_onboard_inspection import draft_for_repository


@pytest.mark.parametrize('command', [
    'npm run ci',
    'npm run install',
    'echo npm run gate',
    'npm run gate || true',
    'npm run gate; true',
    'npm run gate \x00',
    'npm run gate\ntrue',
    'npm run gate $(echo ignored)',
    'npm gate',
    'pnpm ci',
    'yarn ci',
    'bun ci',
    'pnpm gate', 'yarn gate', 'bun gate',
    'npm', 'npm run', "npm run 'gate",
])
def test_package_draft_must_invoke_a_supported_command_or_declared_script(
        tmp_path, box, monkeypatch, command):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'package.json': json.dumps({'scripts': {'gate': 'vitest run'}})},
        {'gate_cmd': {'value': command, 'reason': 'package command', 'evidence': 'package.json'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == ''


@pytest.mark.parametrize('command', [
    'echo scripts/check.py',
    'rm scripts/check.py',
    'python -c scripts/check.py',
    'python scripts/check.py || true',
    'scripts/check.py; true',
])
def test_file_evidence_must_be_invoked_not_just_mentioned(tmp_path, box, monkeypatch, command):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'scripts/check.py': 'raise SystemExit(1)\n'},
        {'gate_cmd': {'value': command, 'reason': 'repository script', 'evidence': 'scripts/check.py'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == ''


@pytest.mark.parametrize('command', [
    'npm run gate', 'npm ci', 'npm install --ignore-scripts',
    'pnpm run gate', 'pnpm install --frozen-lockfile',
    'yarn run gate', 'bun run gate',
])
def test_supported_package_invocations_remain_available(tmp_path, box, monkeypatch, command):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'package.json': json.dumps({'scripts': {'gate': 'vitest run'}})},
        {'gate_cmd': {'value': command, 'reason': 'package command', 'evidence': 'package.json'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == command


@pytest.mark.parametrize('command', ['npm run ci', 'npm run install', 'pnpm run ci', 'npm test'])
def test_declared_scripts_are_distinct_from_package_manager_builtins(tmp_path, box, monkeypatch, command):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'package.json': json.dumps({'scripts': {'ci': 'vitest', 'install': 'node setup.js', 'test': 'vitest'}})},
        {'gate_cmd': {'value': command, 'reason': 'declared script', 'evidence': 'package.json'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == command


@pytest.mark.parametrize('command', ['python scripts/check.py', 'python3 ./scripts/check.py',
                                     './scripts/check.py', 'scripts/check.py'])
def test_direct_or_interpreted_repository_script_is_accepted(tmp_path, box, monkeypatch, command):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'scripts/check.py': 'print("checked")\n'},
        {'gate_cmd': {'value': command, 'reason': 'repository script', 'evidence': 'scripts/check.py'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == command
