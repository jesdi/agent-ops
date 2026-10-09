"""Internal alert identity, presentation eligibility and listener-owned claims.

Conditions and their receipt authority remain owned by runtime delivery. Claims
only record that a host invocation may make its best-effort external effects.
"""
from dispatcher.runtime_work import nonempty
from dispatcher.state import IN_FLIGHT_STAGES, Stage, load


CLAIM_FIELDS = {'pending': 'pending_claim_revision',
                'receipt-resolved': 'receipt_resolution_claim_revision'}


def alert_identity(alert):
    key = 'batch_id' if alert['kind'] == 'delivery-uncertain' else 'message'
    return {'kind': alert['kind'], key: alert[key]}


def valid_identity(identity):
    if not isinstance(identity, dict):
        return False
    fields = {'compatibility': 'message', 'delivery-uncertain': 'batch_id'}
    key = fields.get(identity.get('kind')) if isinstance(identity.get('kind'), str) else None
    return key is not None and set(identity) == {'kind', key} and nonempty(identity[key])


def find_alert(snapshot, identity):
    return next((alert for alert in snapshot['alerts'] if alert_identity(alert) == identity), None)


def confirmed_batch(snapshot, alert):
    return next((batch for batch in snapshot['deliveries']
                 if batch['batch_id'] == alert.get('batch_id') and batch['status'] == 'confirmed'), None)


def phase_eligible(snapshot, alert, phase):
    if alert['kind'] == 'compatibility':
        return phase == 'pending'
    if phase == 'pending':
        return alert['status'] == 'pending'
    return (phase == 'receipt-resolved' and alert['status'] == 'resolved'
            and confirmed_batch(snapshot, alert) is not None)


def current_task(state_dir, binding):
    try:
        task = load(state_dir, binding['target'], binding['issue'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not eligible_task(task):
        return None
    ticket = str(task.ticket_cursor) if task.stage == Stage.IMPLEMENT else ''
    matches = (task.target == binding['target'], task.issue == binding['issue'],
               task.worktree == binding['worktree'], task.continued_stage.value == binding['stage'],
               ticket == binding['ticket'])
    return task if all(matches) else None


def eligible_task(task):
    if task is None or task.stage not in IN_FLIGHT_STAGES or task.park != '':
        return False
    return (type(task.issue) is int and type(task.ticket_cursor) is int
            and task.ticket_cursor >= 0)


def valid_claim(snapshot, event):
    observed = event.get('observed_revision')
    phase = event.get('phase')
    return (type(observed) is int and observed == snapshot['revision']
            and valid_identity(event.get('alert_identity'))
            and isinstance(phase, str) and phase in CLAIM_FIELDS)


def claim_presentation(snapshot, event, state_dir):
    if not valid_claim(snapshot, event):
        return False
    identity, phase = event['alert_identity'], event['phase']
    alert = find_alert(snapshot, identity)
    if alert is None or not phase_eligible(snapshot, alert, phase):
        return False
    field = CLAIM_FIELDS[phase]
    if field in alert.get('presentation', {}) or current_task(state_dir, snapshot['binding']) is None:
        return False
    alert.setdefault('presentation', {})[field] = snapshot['revision'] + 1
    return True


def valid_presentation(snapshot, alert):
    if 'presentation' not in alert:
        return True
    claims = alert['presentation']
    allowed = set(CLAIM_FIELDS.values())
    if alert['kind'] == 'compatibility':
        allowed = {CLAIM_FIELDS['pending']}
    if not isinstance(claims, dict) or not set(claims) <= allowed:
        return False
    if not all(type(value) is int and 0 < value <= snapshot['revision'] for value in claims.values()):
        return False
    return (CLAIM_FIELDS['receipt-resolved'] not in claims
            or phase_eligible(snapshot, alert, 'receipt-resolved'))
