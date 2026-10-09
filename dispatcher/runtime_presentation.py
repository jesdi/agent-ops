"""Best-effort operator presentation of listener-owned runtime conditions.

A newly durable claim permits effects only in this invocation. Retained claim
metadata is never permission to retry an effect whose outcome is unknown.
"""
import json
import logging
from pathlib import Path

from dispatcher import eventlog
from dispatcher.config import Target
from dispatcher.runtime_alerts import (alert_identity, current_task, find_alert,
                                       phase_eligible)
from dispatcher.runtime_http import RuntimeClient
from dispatcher.runtime_snapshots import valid_snapshot
from telegram.notify import Notifier


log = logging.getLogger(__name__)


def _current(client, target, issue):
    snapshot = client.view(target, issue)
    if not valid_snapshot(snapshot) or snapshot['retired']:
        return None
    task = current_task(client.state_dir, snapshot['binding'])
    return (task, snapshot) if task is not None else None


def _same_launch(original, current):
    # The only approved refinement is null -> exact root within this launch.
    refined = dict(original, conversation_id=current['conversation_id'])
    return refined == current and original['conversation_id'] in (None, current['conversation_id'])


def _eligible(client, original, identity, phase):
    current = _current(client, original['target'], original['issue'])
    if current is None:
        return None
    task, snapshot = current
    if not _same_launch(original, snapshot['binding']):
        return None
    alert = find_alert(snapshot, identity)
    if alert is None or not phase_eligible(snapshot, alert, phase):
        return None
    return task, snapshot, alert


def _detail(snapshot, alert, phase):
    detail = dict(binding=snapshot['binding'], alert_identity=alert_identity(alert),
                  phase=phase, message=alert['message'])
    if alert['kind'] == 'delivery-uncertain':
        batch = next(b for b in snapshot['deliveries'] if b['batch_id'] == alert['batch_id'])
        detail['batch_id'] = batch['batch_id']
        if batch['attempts']:
            attempt = batch['attempts'][-1]
            detail['attempt_id'] = attempt['attempt_id']
            if phase == 'receipt-resolved':
                detail['accepted_receipt'] = attempt['receipt']
    return detail


def _note(detail):
    binding, identity = detail['binding'], detail['alert_identity']
    lines = [f"Launch {binding['launch_id']}; stage {binding['stage']}; ticket {binding['ticket'] or '(none)'}.",
             f"{identity['kind']}: {detail['message']}"]
    if identity['kind'] == 'delivery-uncertain':
        lines.append(f"Batch {identity['batch_id']}: receipt is uncertain; automatic resend is held; "
                     "exact bound-history reconciliation continues.")
    return '\n'.join(lines)


def _effects(client, target, notifier, detail):
    original, identity, phase = detail['binding'], detail['alert_identity'], detail['phase']
    eligible = _eligible(client, original, identity, phase)
    if eligible is None:
        return
    task = eligible[0]
    eventlog.append_event(client.state_dir,
        'runtime-alert-pending' if phase == 'pending' else 'runtime-alert-resolved',
        target=original['target'], issue=original['issue'], stage=original['stage'],
        model=task.picks.get(original['stage'], ''), detail=json.dumps(detail))
    if phase != 'pending':
        return
    eligible = _eligible(client, original, identity, phase)
    if eligible is not None:
        notifier.send('runtime_alert', issue=original['issue'], title=eligible[0].title,
                      url=f"https://github.com/{target.repo}/issues/{original['issue']}",
                      note=_note(detail), target=original['target'])


def _present(client, target, notifier, original, identity, phase):
    eligible = _eligible(client, original, identity, phase)
    if eligible is None:
        return
    _, snapshot, alert = eligible
    event = dict(type='alert/presentation-claimed', observed_revision=snapshot['revision'],
                 alert_identity=identity, phase=phase)
    if client.event(snapshot['binding'], event) is True:
        _effects(client, target, notifier, _detail(snapshot, alert, phase))


def present_runtime_alerts(state_dir: str | Path, target: Target, issue: int,
                           notifier: Notifier, *, dry_run: bool = False) -> None:
    """Present eligible current conditions once, without changing task authority."""
    if dry_run:
        return
    client = RuntimeClient(state_dir)
    try:
        current = _current(client, target.name, issue)
        if current is None:
            return
        snapshot = current[1]
        for alert in snapshot['alerts']:
            phase = 'receipt-resolved' if alert.get('status') == 'resolved' else 'pending'
            try:
                _present(client, target, notifier, snapshot['binding'], alert_identity(alert), phase)
            except Exception:
                log.warning('Runtime alert presentation failed for %s#%s', target.name, issue, exc_info=True)
    except Exception:
        log.warning('Runtime alert evidence unavailable for %s#%s', target.name, issue, exc_info=True)
