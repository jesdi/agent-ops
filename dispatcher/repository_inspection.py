"""Bounded, non-executing repository facts and independently validated drafts."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from dispatcher.state import read_regular

COMMANDS = ('setup_cmd', 'gate_cmd', 'verify_cmd')
LIMIT = 262144


def read(root: Path, name: str) -> str:
    """Read only regular files inside the clone; never follow a symlink."""
    path = root / name
    try:
        relative = path.relative_to(root)
        if any((root / Path(*relative.parts[:n])).is_symlink()
               for n in range(1, len(relative.parts) + 1)):
            return ''
        data = read_regular(path, LIMIT)
        return data.decode() if data is not None else ''
    except (OSError, UnicodeError, ValueError):
        return ''


def _document(root: Path, name: str, parser) -> dict:
    try:
        result = parser(read(root, name))
        return result if isinstance(result, dict) else {}
    except (ValueError, yaml.YAMLError, RecursionError):
        return {}


def value(item, source: str = '') -> dict:
    return {'value': item, 'source': source}


def facts(clone: str, repo: str) -> dict:
    root = Path(clone)
    metadata = _document(root, '.backlog/project-meta.json', json.loads)
    skills = _document(root, '.my-skills.json', json.loads).get('skills', {})
    return {'repo': repo, 'state': 'waiting', 'detail': '', 'request_id': '',
            'fields': {name: value('') for name in COMMANDS},
            'board': _board(metadata), 'services': _services(root),
            'secrets': _secrets(root), 'skills': list(skills) if isinstance(skills, dict) else []}


def _board(metadata: dict) -> dict:
    owner, number = metadata.get('owner'), metadata.get('projectNumber')
    if (isinstance(owner, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', owner)
            and type(number) is int and number > 0):
        return value({'owner': owner, 'number': number}, 'rule: .backlog/project-meta.json')
    return value(None)


def _services(root: Path) -> list[dict]:
    found = {}
    names = {'compose.yaml', *(p.name for p in root.glob('docker-compose*.yml'))}
    for name in sorted(names):
        services = _document(root, name, yaml.safe_load).get('services', {})
        if isinstance(services, dict):
            for service in services:
                if isinstance(service, str):
                    found.setdefault(service, value(service, f'rule: {name}'))
    return list(found.values())


def _secrets(root: Path) -> list[dict]:
    names = re.findall(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=',
                       read(root, '.env.example'), re.MULTILINE)
    return [value(name, 'rule: .env.example') for name in dict.fromkeys(names)]


def apply_draft(report: dict, draft: object, clone: str) -> bool:
    if not isinstance(draft, dict):
        return False
    for name in COMMANDS:
        item = _command(draft.get(name), Path(clone))
        if item:
            report['fields'][name] = item
    allowed = {entry['value'] for entry in report['services']}
    report['selected_services'] = _draft_names(draft.get('services'), allowed)
    secret_names = {entry['value'] for entry in report['secrets']}
    extra_secrets = _draft_names(draft.get('secrets'), None, secret_names)
    report['secrets'].extend(extra_secrets)
    return bool(report['selected_services'] or extra_secrets or
                any(x['source'].startswith('draft:') for x in report['fields'].values()))


def _command(raw: object, root: Path) -> dict | None:
    if not isinstance(raw, dict):
        return None
    command, reason, evidence = raw.get('value'), raw.get('reason'), raw.get('evidence')
    if not all(isinstance(x, str) and x.strip() for x in (command, reason, evidence)):
        return None
    path = Path(evidence)
    if path.is_absolute() or '..' in path.parts or not read(root, evidence):
        return None
    if not _references(command, evidence, root):
        return None
    return value(command.strip(), f'draft: {reason.strip()}')


def _references(command: str, evidence: str, root: Path) -> bool:
    """Check the named target/script, rather than merely its manifest."""
    if evidence in command or Path(evidence).name in command:
        return True
    if evidence == 'Makefile':
        target = re.search(r'(?:^|\s)make\s+([A-Za-z0-9_.-]+)', command)
        return bool(target and re.search(r'^' + re.escape(target[1]) + r'\s*:',
                                        read(root, evidence), re.MULTILINE))
    if evidence == 'package.json':
        return _package_command(command, _document(root, evidence, json.loads))
    return False


def _package_command(command: str, manifest: dict) -> bool:
    match = re.search(r'(?:^|\s)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?([A-Za-z0-9_.:-]+)', command)
    if not match:
        return False
    if match[1] in ('install', 'ci'):
        return True
    scripts = manifest.get('scripts', {})
    return isinstance(scripts, dict) and match[1] in scripts


def _draft_names(raw: object, allowed: set | None, exclude: set | None = None) -> list[dict]:
    if not isinstance(raw, list):
        return []
    found = {}
    for entry in raw[:100]:
        item = _draft_name(entry, allowed)
        if item and item['value'] not in (exclude or set()):
            found[item['value']] = item
    return list(found.values())


def _draft_name(raw: object, allowed: set | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name, reason = raw.get('value'), raw.get('reason')
    if not isinstance(name, str) or not isinstance(reason, str) or not reason.strip():
        return None
    if allowed is not None and name not in allowed:
        return None
    if allowed is None and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        return None
    return value(name, f'draft: {reason.strip()}')
