"""Additional presentation regressions through the approved host interfaces."""
import copy
import json
import unittest

from t8_locked.host_support import Host


class RuntimePresentationRegressions(unittest.TestCase):
    def setUp(self):
        self.host = Host(self, proxy=True)

    def test_inventory_problem_keeps_its_claim_after_repeated_observation(self):
        key = self.host.task()
        identity = {'kind': 'compatibility', 'message': 'OWN incomplete inventory page'}
        event = dict(type='inventory', certainty='unknown', workers=[], message=identity['message'])
        self.host.apply(key, event)
        notifier = self.host.present(key)
        admitted = copy.deepcopy(self.host.alert(key, identity))
        self.host.apply(key, event)
        self.host.present(key, notifier)
        self.assertEqual(self.host.view(key)['alerts'], [admitted])
        self.assertEqual(len(notifier.calls), 1)

    def test_invalid_saved_task_identity_cannot_claim_or_notify(self):
        key = self.host.task()
        identity = self.host.compatible(key)
        before = self.host.view(key)
        path, = self.host.generated_task_paths[key]
        original = path.read_bytes()
        for changes in ({'issue': True}, {'ticket_cursor': '1'},
                        {'park': None}, {'park': []}, {'park': 0}):
            with self.subTest(changes=changes):
                task = json.loads(original)
                task.update(changes)
                path.write_text(json.dumps(task))
                notifier = self.host.present(key)
                self.assertEqual(notifier.calls, [])
                self.assertEqual(self.host.view(key), before)
                self.assertIs(self.host.client.event(self.host.binding(key),
                              self.host.claim_event(key, identity)), False)
        path.write_bytes(original)
        self.assertEqual(len(self.host.present(key).calls), 1)

    def test_corrupt_claim_metadata_becomes_unknown_and_never_grants_effects(self):
        key = self.host.task()
        identity = self.host.compatible(key)
        self.host.present(key)
        path = self.host.snapshot_path(key)
        original = path.read_bytes()
        good = json.loads(original)
        malformed = [None, [], {'pending_claim_revision': True},
                     {'pending_claim_revision': 0}, {'pending_claim_revision': -1},
                     {'pending_claim_revision': 1.5}, {'pending_claim_revision': '1'},
                     {'pending_claim_revision': good['revision'] + 1},
                     {'receipt_resolution_claim_revision': good['revision']},
                     {'retry_token': 'not-authority'}]
        for claims in malformed:
            with self.subTest(claims=claims):
                snapshot = copy.deepcopy(good)
                snapshot['alerts'][0]['presentation'] = claims
                path.write_text(json.dumps(snapshot))
                self.assertEqual(self.host.view(key)['binding'], {})
                self.assertEqual(self.host.present(key).calls, [])
        path.write_bytes(original)
        self.assertEqual(self.host.view(key), good)
        self.assertEqual(self.host.present(key).calls, [])
        self.assertEqual(len(self.host.audits(identity)), 1)

    def test_resolution_claim_requires_persisted_own_receipt_after_restart(self):
        key = self.host.task()
        identity = self.host.uncertain(key)
        self.host.ack(key)
        self.host.present(key)
        good = self.host.view(key)
        self.host.listener.restart()
        self.assertEqual(self.host.view(key), good)
        self.assertEqual(self.host.present(key).calls, [])
        # A corrupt stored claim must not turn a pending batch into a receipt.
        path = self.host.snapshot_path(key)
        broken = copy.deepcopy(good)
        broken['alerts'][0]['status'] = 'pending'
        path.write_text(json.dumps(broken))
        self.assertEqual(self.host.view(key)['binding'], {})
        self.assertEqual(self.host.present(key).calls, [])
        self.assertEqual(len(self.host.audits(identity, 'runtime-alert-resolved')), 1)

    def test_malformed_or_unknown_claim_cannot_consume_initial_notice(self):
        key = self.host.task()
        identity = self.host.compatible(key)
        before = self.host.view(key)
        original = self.host.claim_event(key, identity)
        invalid = [{'observed_revision': True}, {'observed_revision': -1},
                   {'phase': []}, {'phase': 'retry'}, {'alert_identity': []},
                   {'alert_identity': {'kind': [], 'message': 'problem'}},
                   {'alert_identity': {'kind': 'compatibility', 'message': ''}},
                   {'alert_identity': dict(identity, token='extra')},
                   {'alert_identity': dict(kind='compatibility', message='absent')}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertIs(self.host.client.event(self.host.binding(key), dict(original, **changes)), False)
                self.assertEqual(self.host.view(key), before)
        self.assertEqual(len(self.host.present(key).calls), 1)
