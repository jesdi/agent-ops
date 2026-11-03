"""Durable onboarding session requests; the dispatcher owns their capacity."""
from __future__ import annotations

import hashlib
import json
import shlex
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, TypedDict

from dispatcher.config import Config
from dispatcher.models import Entry, Order

from dispatcher import herdr
from dispatcher.models import triage_entry
from dispatcher.state import read_regular, state_lock, write_json_atomic

DIRECTORY = 'onboarding-sessions'


class SessionStatus(TypedDict):
    state: Literal['waiting', 'running', 'complete', 'failed', 'withdrawn']
    detail: str
    result: dict | None


def _root(state_dir):
    return Path(state_dir) / DIRECTORY


def _path(state_dir, ident):
    if len(ident) != 64 or any(c not in '0123456789abcdef' for c in ident):
        raise ValueError('invalid onboarding session id')
    return _root(state_dir) / ident / 'request.json'


def _read(path):
    raw = read_regular(path, 1024 * 1024)
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, RecursionError):
        return {}


def ensure_request(state_dir: str | Path, project: str, repository: str, revision: str,
                   clone_path: str, facts: dict, *, generation: int = 0) -> str:
    ident = hashlib.sha256(json.dumps([project, repository, revision, generation]).encode()).hexdigest()
    path = _path(state_dir, ident)
    with state_lock(state_dir, 'onboarding-sessions.lock'):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, dict(id=ident, project=project, repository=repository,
                revision=revision, generation=generation, clone_path=clone_path, facts=facts, state='waiting',
                detail='waiting for the dispatcher', result=None))
    return ident


def status(state_dir: str | Path, ident: str) -> SessionStatus:
    doc = _read(_path(state_dir, ident))
    if doc.get('withdrawn'):
        return dict(state='withdrawn', detail='inspection draft skipped', result=None)
    if not doc:
        return dict(state='failed', detail='inspection request unavailable', result=None)
    return {key: doc[key] for key in ('state', 'detail', 'result')}


def _requests(state_dir):
    return [(path, _read(path)) for path in sorted(_root(state_dir).glob('*/request.json'))]


def running(state_dir: str | Path) -> int:
    """Reserved capacity, including the gap between scheduling and runner startup."""
    return sum(doc.get('state') == 'running' for _, doc in _requests(state_dir))


def request_directory(state_dir: str | Path, ident: str) -> Path:
    return _path(state_dir, ident).parent


def load_request(state_dir: str | Path, ident: str) -> dict:
    return _read(_path(state_dir, ident))


def withdraw(state_dir: str | Path, ident: str) -> None:
    with state_lock(state_dir, 'onboarding-sessions.lock'):
        path = _path(state_dir, ident)
        doc = _read(path)
        if doc:
            doc['withdrawn'] = True
            write_json_atomic(path, doc)


def finish(state_dir: str | Path, ident: str, *, result: dict | None = None,
           error: str = '') -> None:
    """Called after the container exits; releasing capacity is atomic with its result."""
    with state_lock(state_dir, 'onboarding-sessions.lock'):
        path = _path(state_dir, ident)
        doc = _read(path)
        if doc.get('state') == 'running':
            doc.update(state='failed' if error else 'complete', detail=error, result=result)
            write_json_atomic(path, doc)


def _launch(cfg, path, doc, entry):
    doc.update(state='running', detail='inspecting repository',
               started_at=datetime.now(timezone.utc).isoformat(), model=entry.model_id)
    write_json_atomic(path, doc)  # reserve before any external launch
    checkout = str(Path(__file__).resolve().parents[1])
    tab = herdr.Tab.ensure(herdr.SYSTEM_WORKSPACE, 'inspect-' + doc['id'], checkout)
    argv = [sys.executable, '-m', 'dispatcher.onboarding_runner',
            '--state-dir', cfg.state_dir, '--request-id', doc['id'],
            '--memory', cfg.session_memory, '--cpus', cfg.session_cpus,
            '--model', entry.model_id, '--effort', entry.effort]
    if tab is None:
        doc.update(state='waiting', detail='waiting for the session console')
    elif not tab.run(shlex.join(argv) + '; exit'):
        doc['detail'] = 'waiting for session launch acknowledgement'
    write_json_atomic(path, doc)
    return doc['state'] == 'running'


def _wait_reason(cfg, free, occupied, doc, entry):
    if doc['project'] in occupied:
        return 'waiting for this project’s current inspection'
    if free <= 0:
        return f'waiting for a free execution unit (box full: {cfg.capacity}/{cfg.capacity})'
    if entry is None:
        return 'waiting for model usage allowance'
    return ''


def tick(cfg: Config, deps: object, admit: Callable[[str], object], order: Order,
         config_path: str, free: int) -> int:
    """Launch after in-flight work and before claims. Caller owns the free count."""
    entry = triage_entry(cfg.models, admit, order)
    launched = 0
    with state_lock(cfg.state_dir, 'onboarding-sessions.lock'):
        requests = _requests(cfg.state_dir)
        occupied = {d['project'] for _, d in requests if d.get('state') == 'running'}
        for path, doc in requests:
            if doc.get('state') != 'waiting' or doc.get('withdrawn'):
                continue
            reason = _wait_reason(cfg, free - launched, occupied, doc, entry)
            if reason:
                doc['detail'] = reason
                write_json_atomic(path, doc)
            elif _launch(cfg, path, doc, entry):
                occupied.add(doc['project'])
                launched += 1
    return launched


def _overdue(doc, now):
    if doc.get('state') != 'running':
        return False
    try:
        started = datetime.fromisoformat(doc['started_at'])
        return (now - started).total_seconds() > 15 * 60
    except (ValueError, KeyError, TypeError):
        return True


def _stop_runner(name):
    tab = herdr.Tab.find(name)
    if tab is not None:
        return tab.close()
    labels = herdr.tab_labels()
    return labels is not None and name not in labels


def _stop_container(name, run):
    try:
        exists = run(['podman', 'container', 'exists', name], capture_output=True, timeout=30)
        if exists.returncode == 1:
            return True
        if exists.returncode != 0:
            return False
        return run(['podman', 'kill', name], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def reconcile(state_dir: str | Path, *, run: Callable | None = None,
              now: datetime | None = None) -> None:
    """Bound interrupted runners, never guessing that an unavailable daemon is idle."""
    run = run or subprocess.run
    now = now or datetime.now(timezone.utc)
    with state_lock(state_dir, 'onboarding-sessions.lock'):
        for path, doc in _requests(state_dir):
            if not _overdue(doc, now):
                continue
            name = 'inspect-' + doc['id']
            if _stop_runner(name) and _stop_container(name, run):
                doc.update(state='failed', detail='inspection interrupted before a result was saved', result=None)
                write_json_atomic(path, doc)
