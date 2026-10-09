"""Measure alert claims through trusted host and HTTP public boundaries."""
import unittest

from dispatcher.runtime_control import RuntimeControl
from dispatcher.runtime_http import RuntimeClient, UnixHTTP
from dispatcher.state import Stage, TaskState, save
from tests.runtime_listener import launch_listener
from t8_locked.host_support import Host, wire_request


class TrustedPresentationClaims(unittest.TestCase):
    def setUp(self):
        self.host = Host(self, proxy=True)

    def test_trusted_host_claims_only_current_eligible_condition_once(self):
        key = self.host.task()
        identity = self.host.compatible(key)
        # Trusted direct host boundary becomes the sole writer after shutdown.
        self.host.listener.close()
        control = RuntimeControl(self.host.state_dir)
        binding = self.host.binding(key)
        before = control.view(*key)
        event = self.host.claim_event(key, identity)
        invalid = [{'observed_revision': True}, {'phase': []}, {'alert_identity': []},
                   {'observed_revision': before['revision'] - 1}, {'phase': 'retry'},
                   {'alert_identity': dict(kind='compatibility', message='')},
                   {'alert_identity': {'kind': [], 'message': 'problem'}},
                   {'alert_identity': dict(kind='other', message='problem')},
                   {'alert_identity': dict(identity, token='extra')},
                   {'alert_identity': dict(kind='compatibility', message='absent')},
                   {'phase': 'receipt-resolved'}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertIs(control.event(binding, dict(event, **changes)), False)
                self.assertEqual(control.view(*key), before)
        self.host.save(key, park='operator-pause')
        self.assertIs(control.event(binding, event), False)
        self.host.save(key, park='')
        self.assertIs(control.event(binding, event), True)
        admitted = control.view(*key)
        self.assertEqual(admitted['alerts'][0]['presentation'],
                         {'pending_claim_revision': before['revision'] + 1})
        self.assertIs(control.event(binding, self.host.claim_event(key, identity)), False)
        self.assertEqual(control.view(*key), admitted)
        self.host.listener.start()
        self.assertEqual(self.host.present(key).calls, [])

    def test_trusted_host_resolution_claim_requires_own_accepted_receipt(self):
        key = self.host.task()
        identity = self.host.uncertain(key)
        self.host.listener.close()
        control = RuntimeControl(self.host.state_dir)
        binding = self.host.binding(key)
        self.assertIs(control.event(binding, self.host.claim_event(key, identity, 'receipt-resolved')), False)
        self.assertIs(control.event(binding, self.host.claim_event(key, identity)), True)
        before_receipt = control.view(*key)
        self.assertEqual(before_receipt['deliveries'][0]['status'], 'pending')
        attempt = self.host.attempt(key, 'D')
        self.assertIs(control.event(binding, dict(type='delivery/ack', batch_id='D',
            attempt_id=attempt['attempt_id'], client_message_id=attempt['client_message_id'],
            thread_id=binding['conversation_id'], turn_id=attempt['expected_turn_id'])), True)
        event = self.host.claim_event(key, identity, 'receipt-resolved')
        self.assertIs(control.event(binding, event), True)
        resolved = control.view(*key)
        self.assertEqual(resolved['alerts'][0]['presentation']['receipt_resolution_claim_revision'],
                         event['observed_revision'] + 1)
        self.assertEqual(resolved['main'], before_receipt['main'])
        self.assertEqual(resolved['wait'], before_receipt['wait'])
        self.assertIs(control.event(binding, self.host.claim_event(key, identity, 'receipt-resolved')), False)
        self.assertEqual(control.view(*key), resolved)


def test_dead_service_rejects_claim_without_consuming_notice(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('fixture', 1, 'review', worktree='/owned')['binding']
    save(tmp_path, TaskState(target='fixture', issue=1, stage=Stage.REVIEW, worktree='/owned',
                            slot=1, branch='owned', title='OWN task', updated_at='2026-10-09'))
    identity = dict(kind='compatibility', message='OWN unsupported control')
    assert control.event(binding, dict(type='control/unknown', message=identity['message']))
    assert control.event(binding, dict(type='service', status='dead'))
    dead = control.view('fixture', 1)
    claim = dict(type='alert/presentation-claimed', observed_revision=dead['revision'],
                 alert_identity=identity, phase='pending')
    assert control.event(binding, claim) is False
    assert control.view('fixture', 1) == dead
    assert control.event(binding, dict(type='service', status='live')) is False
    assert control.view('fixture', 1) == dead


def test_http_claim_requires_host_while_bound_events_remain_available(tmp_path, launch_listener):
    client = RuntimeClient(tmp_path)
    binding = client.prepare('fixture', 1, 'review', worktree='/owned')['binding']
    save(tmp_path, TaskState(target='fixture', issue=1, stage=Stage.REVIEW, worktree='/owned',
                            slot=1, branch='owned', title='OWN task', updated_at='2026-10-09'))
    connection = UnixHTTP(tmp_path)
    path = connection.path
    connection.close()
    assert wire_request(path, '/runtime/view', dict(target='fixture', issue=1)) == (403, None)
    assert wire_request(path, '/runtime/prepare', dict(target='foreign', issue=1, stage='review')) == (403, None)
    assert wire_request(path, '/runtime/retire', dict(binding=binding, revision=0)) == (403, None)
    assert wire_request(path, '/runtime/absent', {}) == (404, None)
    assert wire_request(path, '/runtime/event', dict(binding=binding, event=[])) == (200, False)
    identity = dict(kind='compatibility', message='OWN unsupported control')
    event = dict(type='control/unknown', message=identity['message'])
    assert wire_request(path, '/runtime/event', dict(binding=binding, event=event)) == (200, True)
    before = client.view('fixture', 1)
    assert wire_request(path, '/runtime/view', dict(target='fixture', issue=1,
                        launch_id=binding['launch_id'])) == (200, before)
    claim = dict(type='alert/presentation-claimed', observed_revision=before['revision'],
                 alert_identity=identity, phase='pending')
    payload = dict(binding=binding, event=claim)
    assert wire_request(path, '/runtime/event', payload) == (403, None)
    assert wire_request(path, '/runtime/event', payload, 'wrong-host-token') == (403, None)
    assert client.view('fixture', 1) == before
    assert client.event(binding, claim) is True
    claimed = client.view('fixture', 1)
    assert claimed['alerts'][0]['presentation'] == {
        'pending_claim_revision': before['revision'] + 1}
    claim['observed_revision'] = claimed['revision']
    assert client.event(binding, claim) is False
    assert client.view('fixture', 1) == claimed
