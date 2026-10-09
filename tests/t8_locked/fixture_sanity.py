"""Focused OWN infrastructure checks, separate from the unchanged 98 H cases.

The RPC peer is a bounded host stand-in and supplies no genuine native evidence.
"""
import copy
import unittest

from host_support import Host


class FixtureSanity(unittest.TestCase):
    def setUp(self):
        self.host = Host(self, proxy=True)

    def test_real_listener_pending_ack_and_history_are_independent(self):
        key = self.host.task()
        self.host.uncertain(key, "A")
        self.host.uncertain(key, "B")
        self.host.ack(key, "A")
        self.assertEqual(self.host.delivery(key, "A")["status"], "confirmed")
        self.assertEqual(self.host.delivery(key, "B")["status"], "pending")
        self.host.history_receipt(key, "B")
        self.assertEqual(self.host.delivery(key, "B")["status"], "confirmed")
        self.assertTrue(self.host.listener.proxy.records)

    def test_real_controller_attachment_fixture_reconnects_without_input(self):
        from controller_peer import two_public_attachments
        key = self.host.task()
        identity = self.host.uncertain(key)
        before = copy.deepcopy(self.host.alert(key, identity))
        two_public_attachments(self, self.host, key)
        self.assertEqual(self.host.alert(key, identity), before)

    def test_owned_listener_restart_has_new_inode_and_preserves_binding(self):
        key = self.host.task()
        binding = copy.deepcopy(self.host.binding(key))
        self.host.listener.restart()
        self.assertEqual(self.host.binding(key), binding)
