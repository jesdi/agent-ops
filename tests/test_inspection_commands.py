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
    'npm run gate', 'npm ci', 'npm ci --ignore-scripts', 'npm install --ignore-scripts',
    'pnpm run gate', 'pnpm install --frozen-lockfile',
    'yarn run gate', 'bun run gate', 'yarn install', 'bun install',
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


@pytest.mark.parametrize('command', [
    'npm run gate --prefix nonexistent',
    'npm run gate --prefix=nonexistent',
    'pnpm run gate -C nonexistent',
    'yarn run gate --cwd nonexistent',
    'bun run gate --filter other',
    'npm test --workspaces',
    'npm run gate --script-shell other',
    'npm run gate extra',
    'npm install --prefix nonexistent',
    'npm ci --workspace other',
    'npm install added-package',
    'npm install --frozen-lockfile',
    'pnpm install --dir nonexistent',
    'yarn install --cwd nonexistent',
    'bun install --cwd nonexistent',
])
def test_package_manager_arguments_cannot_redirect_verified_evidence(tmp_path, box, monkeypatch, command):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'package.json': json.dumps({'scripts': {'gate': 'vitest run', 'test': 'vitest run'}})},
        {'gate_cmd': {'value': command, 'reason': 'root package script', 'evidence': 'package.json'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == ''


@pytest.mark.parametrize('command', [
    'npm run gate -- --prefix nonexistent',
    'pnpm run gate -- --prefix nonexistent',
    'yarn run gate -- --prefix nonexistent',
    'bun run gate -- --prefix nonexistent',
    'npm test -- --prefix nonexistent',
])
def test_explicit_separator_passes_literal_arguments_to_the_declared_script(tmp_path, box, monkeypatch, command):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'package.json': json.dumps({'scripts': {'gate': 'vitest run', 'test': 'vitest run'}})},
        {'gate_cmd': {'value': command, 'reason': 'script arguments', 'evidence': 'package.json'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == command


@pytest.mark.parametrize(('name', 'script'), [
    ('--prefix', 'vitest'), ('gate', None), ('gate', 42),
    ('gate', []), ('gate', {}), ('gate', ''), ('gate', '   '),
])
def test_package_draft_requires_a_nonoption_script_name_and_nonempty_string(
        tmp_path, box, monkeypatch, name, script):
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'package.json': json.dumps({'scripts': {name: script}})},
        {'gate_cmd': {'value': f'npm run {name}', 'reason': 'declared script', 'evidence': 'package.json'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == ''


@pytest.mark.parametrize(('command', 'evidence', 'contents'), [
    ('make --help', 'Makefile', '--help:\n\tpytest\n'),
    ('python -c pass', '-c', 'print("checked")\n'),
])
def test_option_tokens_cannot_masquerade_as_repository_targets(
        tmp_path, box, monkeypatch, command, evidence, contents):
    rec = draft_for_repository(tmp_path, box, monkeypatch, {evidence: contents},
        {'gate_cmd': {'value': command, 'reason': 'repository target', 'evidence': evidence}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == ''


def test_explicit_path_to_a_file_with_a_leading_dash_is_unambiguous(tmp_path, box, monkeypatch):
    rec = draft_for_repository(tmp_path, box, monkeypatch, {'-c': 'print("checked")\n'},
        {'gate_cmd': {'value': 'python ./-c', 'reason': 'repository script', 'evidence': '-c'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == 'python ./-c'


def test_implicit_make_draft_cannot_use_a_shadowed_makefile(tmp_path, box, monkeypatch):
    import subprocess
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'Makefile': 'gate:\n\t@printf "verified\\n"\n', 'GNUmakefile': 'other:\n\t@true\n'},
        {'gate_cmd': {'value': 'make gate', 'reason': 'Makefile target gate', 'evidence': 'Makefile'}})
    repository = rec.proposal['repositories'][0]
    actual = subprocess.run(['make', 'gate'], cwd=repository['clone_path'],
                            capture_output=True, text=True, timeout=10)
    assert actual.returncode == 2
    assert 'No rule to make target' in actual.stderr
    assert repository['gate_cmd'] == ''


def test_explicit_makefile_draft_bypasses_default_precedence(tmp_path, box, monkeypatch):
    import subprocess
    command = 'make -f Makefile gate'
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'Makefile': 'gate:\n\t@printf "verified\\n"\n', 'GNUmakefile': 'other:\n\t@true\n'},
        {'gate_cmd': {'value': command, 'reason': 'explicit Makefile target', 'evidence': 'Makefile'}})
    repository = rec.proposal['repositories'][0]
    # Run under `make test`, an inherited MAKELEVEL makes the child print "Entering directory".
    for name in ('MAKELEVEL', 'MAKEFLAGS', 'MFLAGS'):
        monkeypatch.delenv(name, raising=False)
    actual = subprocess.run(['make', '-f', 'Makefile', 'gate'], cwd=repository['clone_path'],
                            capture_output=True, text=True, timeout=10)
    assert actual.returncode == 0
    assert actual.stdout == 'verified\n'
    assert repository['gate_cmd'] == command


@pytest.mark.parametrize(('command', 'expected'), [
    ('make gate', ''), ('make -f Makefile gate', 'make -f Makefile gate'),
])
def test_lowercase_makefile_precedence_from_filesystem_entries(
        tmp_path, box, monkeypatch, command, expected):
    from pathlib import Path
    real_entries = Path.iterdir

    def entries_with_lowercase_makefile(directory):
        entries = list(real_entries(directory))
        if directory.name == 'analytics':
            entries.append(directory / 'makefile')
        return iter(entries)

    # Model two distinct names even on a case-insensitive development volume.
    monkeypatch.setattr(Path, 'iterdir', entries_with_lowercase_makefile)
    rec = draft_for_repository(tmp_path, box, monkeypatch,
        {'Makefile': 'gate:\n\t@printf "verified\\n"\n'},
        {'gate_cmd': {'value': command, 'reason': 'Makefile target', 'evidence': 'Makefile'}})
    assert rec.proposal['repositories'][0]['gate_cmd'] == expected


@pytest.mark.parametrize(('makefile', 'runs'), [
    ('gate := 1\nall:\n\t@true\n', False),
    ('gate ::= 1\nall:\n\t@true\n', False),
    ('gate::\n\t@true\n', True),
])
def test_make_target_evidence_must_be_a_rule_not_an_assignment(
        tmp_path, box, monkeypatch, makefile, runs):
    import subprocess
    rec = draft_for_repository(tmp_path, box, monkeypatch, {'Makefile': makefile},
        {'gate_cmd': {'value': 'make gate', 'reason': 'Makefile target gate', 'evidence': 'Makefile'}})
    repository = rec.proposal['repositories'][0]
    actual = subprocess.run(['make', 'gate'], cwd=repository['clone_path'],
                            capture_output=True, text=True, timeout=10)
    assert (actual.returncode == 0) is runs
    assert repository['gate_cmd'] == ('make gate' if runs else '')
