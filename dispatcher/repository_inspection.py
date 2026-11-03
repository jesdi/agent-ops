"""Bounded, non-executing repository facts and independently validated drafts."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Literal
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


@dataclass(frozen=True)
class Finding:
    value: str
    origin: Literal["rule", "draft"]
    reason: str

    def view(self) -> dict:
        return value(self.value, f"{self.origin}: {self.reason}")


@dataclass(frozen=True)
class Facts:
    board: dict | None = None
    services: list[Finding] = field(default_factory=list)
    secrets: list[Finding] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict) -> Facts:
        return cls(raw.get("board"), _findings(raw.get("services", [])),
                   _findings(raw.get("secrets", [])), raw.get("skills", []))


@dataclass(frozen=True)
class Draft:
    commands: dict[str, Finding] = field(default_factory=dict)
    services: list[Finding] = field(default_factory=list)
    secrets: list[Finding] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.commands or self.services or self.secrets)

    @classmethod
    def from_json(cls, raw: dict) -> Draft:
        return cls({key: Finding(**item) for key, item in raw.get("commands", {}).items()},
                   _findings(raw.get("services", [])), _findings(raw.get("secrets", [])))


@dataclass
class RepositoryInspection:
    repo: str
    facts: Facts
    draft: Draft = field(default_factory=Draft)
    state: Literal["waiting", "complete", "skipped"] = "waiting"
    request_id: str = ""
    detail: str = ""
    error: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> RepositoryInspection:
        return cls(**{**raw, "facts": Facts.from_json(raw["facts"]),
                      "draft": Draft.from_json(raw.get("draft", {}))})

    def complete(self, result: object, clone: str, error: str = "") -> None:
        self.draft = validate_draft(self.facts, result, clone)
        self.state = "complete"
        self.error = error or ("" if self.draft.valid else "no valid result")
        self.detail = ""

    def skip(self) -> None:
        self.state, self.detail, self.error = "skipped", "fill in by hand", ""

    def view(self, commands: dict, board: dict | None) -> dict:
        return {"repo": self.repo, "state": self.state, "request_id": self.request_id,
                "detail": self.warning or self.detail,
                "fields": command_values(self.draft, commands),
                "board": board_value(self.facts, board),
                "services": [x.view() for x in self.facts.services],
                "selected_services": selected_services(self.draft, commands),
                "secrets": [x.view() for x in self.facts.secrets + self.draft.secrets],
                "skills": self.facts.skills}

    @property
    def warning(self) -> str:
        return f"draft unavailable: {self.error}" if self.error else ""


@dataclass
class Inspection:
    repositories: list[RepositoryInspection] = field(default_factory=list)
    manual: bool = False

    @classmethod
    def from_json(cls, raw: dict) -> Inspection:
        return cls([RepositoryInspection.from_json(x) for x in raw.get("repositories", [])],
                   raw.get("manual", False))


def _findings(raw: list[dict]) -> list[Finding]:
    return [Finding(**item) for item in raw]


def value(item, source: str = '') -> dict:
    return {'value': item, 'source': source}


def command_values(draft: Draft, overrides: dict) -> dict:
    return {key: _command_value(draft.commands.get(key), overrides.get(key)) for key in COMMANDS}


def _command_value(finding: Finding | None, override: str | None) -> dict:
    if override is not None:
        return value(override, "you")
    return finding.view() if finding else value("")


def selected_services(draft: Draft, overrides: dict) -> list[dict]:
    selected = overrides.get("services")
    return [value(x, "you") for x in selected] if selected is not None else [x.view() for x in draft.services]


def board_value(found: Facts, override: dict | None) -> dict:
    if override is not None:
        return value(override, "you")
    return value(found.board, "rule: .backlog/project-meta.json" if found.board else "")


def facts(clone: str) -> Facts:
    root = Path(clone)
    metadata = _document(root, '.backlog/project-meta.json', json.loads)
    skills = _document(root, '.my-skills.json', json.loads).get('skills', {})
    return Facts(_board(metadata), _services(root), _secrets(root),
                 list(skills) if isinstance(skills, dict) else [])


def _board(metadata: dict) -> dict | None:
    owner, number = metadata.get('owner'), metadata.get('projectNumber')
    if (isinstance(owner, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', owner)
            and type(number) is int and number > 0):
        return {'owner': owner, 'number': number}
    return None


def _services(root: Path) -> list[Finding]:
    found = {}
    names = {'compose.yaml', *(p.name for p in root.glob('docker-compose*.yml'))}
    for name in sorted(names):
        services = _document(root, name, yaml.safe_load).get('services', {})
        if isinstance(services, dict):
            for service in services:
                if isinstance(service, str):
                    found.setdefault(service, Finding(service, 'rule', name))
    return list(found.values())


def _secrets(root: Path) -> list[Finding]:
    names = re.findall(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=',
                       read(root, '.env.example'), re.MULTILINE)
    return [Finding(name, 'rule', '.env.example') for name in dict.fromkeys(names)]


def validate_draft(found: Facts, raw: object, clone: str) -> Draft:
    if not isinstance(raw, dict):
        return Draft()
    commands = {name: item for name in COMMANDS
                if (item := _command(raw.get(name), Path(clone))) is not None}
    allowed = {entry.value for entry in found.services}
    names = {entry.value for entry in found.secrets}
    return Draft(commands, _draft_names(raw.get('services'), allowed),
                 _draft_names(raw.get('secrets'), None, names))


def _command(raw: object, root: Path) -> Finding | None:
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
    return Finding(command.strip(), 'draft', reason.strip())


def _references(command: str, evidence: str, root: Path) -> bool:
    """Validate manifests before checking exact repository-relative path tokens."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if Path(evidence).name == 'Makefile':
        return _make_command(tokens, read(root, evidence), evidence)
    if Path(evidence).name == 'package.json':
        return _package_command(command, _document(root, evidence, json.loads))
    return evidence in [token.removeprefix('./') for token in tokens]


def _make_command(tokens: list[str], contents: str, evidence: str) -> bool:
    if not tokens or tokens[0] not in ('make', 'gmake'):
        return False
    args = tokens[1:]
    if args[:2] == ['-f', evidence]:
        args = args[2:]
    if not args or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', x) for x in args):
        return False
    return all(re.search(r'^' + re.escape(target) + r'\s*:', contents, re.MULTILINE)
               for target in args)


def _package_command(command: str, manifest: dict) -> bool:
    match = re.search(r'(?:^|\s)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?([A-Za-z0-9_.:-]+)', command)
    if not match:
        return False
    if match[1] in ('install', 'ci'):
        return True
    scripts = manifest.get('scripts', {})
    return isinstance(scripts, dict) and match[1] in scripts


def _draft_names(raw: object, allowed: set | None, exclude: set | None = None) -> list[Finding]:
    if not isinstance(raw, list):
        return []
    found = {}
    for entry in raw[:100]:
        item = _draft_name(entry, allowed)
        if item and item.value not in (exclude or set()):
            found[item.value] = item
    return list(found.values())


def _draft_name(raw: object, allowed: set | None) -> Finding | None:
    if not isinstance(raw, dict):
        return None
    name, reason = raw.get('value'), raw.get('reason')
    if not isinstance(name, str) or not isinstance(reason, str) or not reason.strip():
        return None
    if allowed is not None and name not in allowed:
        return None
    if allowed is None and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        return None
    return Finding(name, 'draft', reason.strip())
