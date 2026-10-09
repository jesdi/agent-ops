"""Independent locked T8 H acceptance cases at the published host seam.

Each generated unittest method remains independently addressable. All product
interaction is through the declared host/listener/recording external boundaries.
Genuine native requirements are deliberately outside these host cases.
"""
from __future__ import annotations

import concurrent.futures
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest

from host_support import (Host, RecordingNotifier, identity_in_detail,
                          presentation_capability, values, wire_request,
                          without_presentation)


class HostAcceptance(unittest.TestCase):
    def setUp(self):
        self.host = Host(self, proxy=True)

    def pending(self, *, batch="D", name="fixture", issue=1):
        key = self.host.task(name, issue)
        identity = self.host.uncertain(key, batch)
        self.assertEqual(self.host.alert(key, identity)["status"], "pending")
        self.assertEqual(self.host.delivery(key, batch)["status"], "pending")
        self.assertNotIn("pending_claim_revision", self.host.alert(key, identity).get("presentation", {}))
        return key, identity

    def compatibility(self, *, stage="IMPLEMENT", unbound=False, message="OWN control format unsupported"):
        key = self.host.task(stage=stage, unbound=unbound)
        identity = self.host.compatible(key, message)
        self.assertNotIn("status", self.host.alert(key, identity))
        return key, identity

    def assert_claim(self, key, identity, field="pending_claim_revision"):
        revision = self.host.alert(key, identity).get("presentation", {}).get(field)
        self.assertIsInstance(revision, int, "missing behavior: durable presentation phase claim")
        self.assertNotIsInstance(revision, bool)
        self.assertGreater(revision, 0)
        self.assertLessEqual(revision, self.host.view(key)["revision"])
        return revision

    def assert_pending_effect(self, key, identity, notifier):
        self.assert_claim(key, identity)
        self.assertEqual(len(notifier.calls), 1, "new pending claim must invoke one initial notification")
        self.assertEqual(notifier.calls[0]["template"], "runtime_alert")
        records = self.host.audits(identity, "runtime-alert-pending")
        self.assertEqual(len(records), 1, "new pending claim must append real task audit history")
        self.assertEqual((records[0]["target"], records[0]["issue"]), key)

    def assert_no_effect(self, identity, notifier):
        self.assertEqual(notifier.calls, [])
        self.assertEqual(self.host.audits(identity), [])

    def assert_original_binding(self, record, binding):
        detail = json.loads(record["detail"])
        self.assertTrue(any(isinstance(node, dict) and all(node.get(k) == v for k, v in binding.items())
                            for node in values(detail)),
                        "typed JSON detail must retain the ORIGINAL complete binding")

    def test_H01_pending_claim_audit_notice_preserves_pending_hold(self):
        key, identity = self.pending()
        before = self.host.view(key)
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(self.host.alert(key, identity)["status"], "pending")
        self.assertEqual(self.host.delivery(key, "D")["status"], "pending")
        self.assertEqual(without_presentation(self.host.view(key)), without_presentation(before))

    def test_H02_repeated_checks_and_changed_diagnostic_keep_one_claim(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        claim = self.assert_claim(key, identity)
        for index, message in enumerate(["first new diagnostic", "second new diagnostic", "second new diagnostic"]):
            self.host.uncertainty(key, "D", message, repeated=index == 2)
            self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(self.assert_claim(key, identity), claim)

    def test_H02_delivery_before_compatibility_observations_stay_independent(self):
        key, identity = self.pending()
        other = self.host.compatible(key, "lexically-before-D")
        notifier = self.host.present(key)
        self.host.uncertainty(key, "D", "lexically-after-compatibility")
        self.host.present(key, notifier)
        self.assertEqual(len(notifier.calls), 2)
        self.assertEqual(len(self.host.audits(identity, "runtime-alert-pending")), 1)
        self.assertEqual(len(self.host.audits(other, "runtime-alert-pending")), 1)

    def test_H02_compatibility_before_delivery_observations_stay_independent(self):
        key = self.host.task()
        other = self.host.compatible(key, "OWN first compatibility observation")
        self.host.apply(key, {"type": "turn/recovered", "thread_id": self.host.root_id(key),
                              "turn_id": "active-main", "status": "inProgress"})
        identity = self.host.uncertain(key)
        notifier = self.host.present(key)
        claim = self.assert_claim(key, identity)
        self.host.compatible(key, other["message"], repeated=True)
        self.host.uncertainty(key, "D", "OWN new diagnostic in opposite condition-observation order")
        self.host.present(key, notifier)
        self.assertEqual(len(notifier.calls), 2)
        self.assertEqual(self.assert_claim(key, identity), claim)
        self.assertEqual(len(self.host.audits(identity, "runtime-alert-pending")), 1)
        self.assertEqual(len(self.host.audits(other, "runtime-alert-pending")), 1)

    def test_H03_controller_attachment_replacement_keeps_claim_identity(self):
        # OWN scripted controller-RPC peer is a host stand-in, never native proof.
        from controller_peer import two_public_attachments
        key, identity = self.pending()
        notifier = self.host.present(key)
        admitted = copy.deepcopy(self.host.alert(key, identity))
        two_public_attachments(self, self.host, key)
        self.host.present(key, notifier)
        current = self.host.alert(key, identity)
        self.assertEqual({k: current[k] for k in ["kind", "batch_id", "status", "presentation"]},
                         {k: admitted[k] for k in ["kind", "batch_id", "status", "presentation"]})
        self.assert_pending_effect(key, identity, notifier)

    def test_H04_new_presenter_process_does_not_reuse_durable_claim(self):
        key, identity = self.pending()
        first = self.host.present_process(key)
        stdout, stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, stderr + stdout)
        self.assertEqual(len(self.host.process_calls()), 1)
        claim = self.assert_claim(key, identity)
        second = self.host.present_process(key)
        stdout, stderr = second.communicate(timeout=10)
        self.assertEqual(second.returncode, 0, stderr + stdout)
        self.assertEqual(len(self.host.process_calls()), 1)
        self.assertEqual(self.assert_claim(key, identity), claim)

    def test_H05_listener_restart_preserves_claim_without_repeat(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        admitted = copy.deepcopy(self.host.alert(key, identity))
        old_inode, new_inode = self.host.listener.restart()
        self.assertNotEqual(old_inode, new_inode, "listener restart must create a new owned socket inode")
        self.host.present(key, notifier)
        self.assertEqual(self.host.alert(key, identity), admitted)
        self.assert_pending_effect(key, identity, notifier)

    def test_H06_lost_real_claim_reply_does_not_authorize_effect_or_retry(self):
        key, identity = self.pending()
        self.host.listener.proxy.drop_new_claim_reply = True
        notifier = self.host.present(key)
        self.assertTrue(self.host.listener.proxy.claim_committed.is_set())
        self.assert_claim(key, identity)
        self.assert_no_effect(identity, notifier)
        self.host.present(key, notifier)
        self.assert_no_effect(identity, notifier)

    def test_H07_crash_after_durable_claim_before_effect_is_not_retried(self):
        key, identity = self.pending()
        release = threading.Event()
        self.host.listener.proxy.release_reply = release
        process = self.host.present_process(key)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertTrue(self.host.listener.proxy.claim_committed.wait(5), "real claim must commit before owned process death")
        self.assert_claim(key, identity)
        process.terminate()
        process.communicate(timeout=5)
        release.set()
        self.assertEqual(self.host.process_calls(), [])
        self.assertEqual(self.host.audits(identity), [])
        notifier = self.host.present(key)
        self.assert_no_effect(identity, notifier)

    def test_H08_real_log_append_failure_never_reopens_presentation(self):
        key, identity = self.pending()
        task_before = self.host.state.load(self.host.state_dir, *key)
        path = self.host.state_dir / "events.jsonl"
        backup = self.host.state_dir / "owned-events-before-fault.jsonl"
        if path.exists():
            path.rename(backup)
        path.mkdir()  # Public filesystem fault; append_event still runs unchanged.
        notifier = self.host.present(key)
        claim = self.assert_claim(key, identity)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), task_before)
        self.assertEqual(self.host.alert(key, identity)["status"], "pending")
        path.rmdir()
        if backup.exists():
            backup.rename(path)
        calls = copy.deepcopy(notifier.calls)
        self.host.present(key, notifier)
        self.assertEqual(notifier.calls, calls)
        self.assertEqual(self.host.audits(identity), [])
        self.assertEqual(self.assert_claim(key, identity), claim)

    def test_H09_zero_notifier_return_is_not_receipt_or_retry_authority(self):
        key, identity = self.pending()
        notifier = RecordingNotifier(return_id=0)
        before = self.host.view(key)
        self.host.present(key, notifier)
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(without_presentation(self.host.view(key)), without_presentation(before))

    def test_H10_notifier_timeout_retains_hold_and_never_retries(self):
        key, identity = self.pending()
        notifier = RecordingNotifier(error=TimeoutError("OWN external notifier timeout"))
        before = self.host.view(key)
        task = self.host.state.load(self.host.state_dir, *key)
        self.host.present(key, notifier)
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(without_presentation(self.host.view(key)), without_presentation(before))
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), task)

    def resolved_independently(self, mode):
        key, identity = self.pending()
        second = self.host.uncertain(key, "E")
        compatible = self.host.compatible(key, "independent compatibility hold")
        # Recover authoritative active visibility after compatibility uncertainty.
        active = self.host.attempt(key, "E")["expected_turn_id"]
        self.host.apply(key, {"type": "turn/recovered", "thread_id": self.host.root_id(key),
                              "turn_id": active, "status": "inProgress"})
        operator = "independent-operator-" + self.host.binding(key)["launch_id"]
        self.assertIs(self.host.client.accept_input(self.host.binding(key), operator), True)
        notifier = self.host.present(key)
        before_other = copy.deepcopy(self.host.alert(key, second))
        compatibility_before = copy.deepcopy(self.host.alert(key, compatible))
        main_before = copy.deepcopy(self.host.view(key)["main"])
        if mode == "ack":
            self.host.ack(key)
        else:
            self.host.history_receipt(key)
        self.host.present(key, notifier)
        self.assertEqual(self.host.delivery(key, "D")["status"], "confirmed")
        self.assertEqual(self.host.alert(key, identity)["status"], "resolved")
        self.assert_claim(key, identity, "receipt_resolution_claim_revision")
        self.assertEqual(len(self.host.audits(identity, "runtime-alert-resolved")), 1)
        self.assertEqual(len(notifier.calls), 3, "receipt resolution must not add a push")
        self.assertEqual(self.host.alert(key, second), before_other)
        self.assertEqual(self.host.alert(key, compatible), compatibility_before)
        self.assertEqual(self.host.view(key)["inputs"][operator]["status"], "pending")
        self.assertEqual(self.host.view(key)["main"], main_before)

    def test_H11_ack_resolves_only_its_batch_without_resolution_push(self):
        self.resolved_independently("ack")

    def test_H12_history_resolves_only_its_batch_without_resolution_push(self):
        self.resolved_independently("history")

    def test_H13_duplicate_ack_never_repeats_confirmation_or_effect(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        self.host.ack(key)
        self.host.present(key, notifier)
        after = copy.deepcopy(self.host.view(key))
        self.host.ack(key, repeated=True)
        self.host.present(key, notifier)
        self.assertEqual(self.host.view(key), after)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(len(self.host.audits(identity, "runtime-alert-resolved")), 1)

    def test_H13_duplicate_history_never_repeats_confirmation_or_effect(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        self.host.history_receipt(key)
        self.host.present(key, notifier)
        after = copy.deepcopy(self.host.view(key))
        self.host.history_receipt(key, repeated=True)
        self.host.present(key, notifier)
        self.assertEqual(self.host.view(key), after)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(len(self.host.audits(identity, "runtime-alert-resolved")), 1)

    def test_H13_stale_ack_does_not_resolve_current_batch(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        event = {"type": "delivery/ack", "batch_id": "D", "attempt_id": "foreign-attempt",
                 "client_message_id": "foreign-client", "thread_id": self.host.root_id(key),
                 "turn_id": self.host.attempt(key, "D")["expected_turn_id"]}
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(self.host.binding(key), event), False)
        self.host.present(key, notifier)
        self.assertEqual(self.host.view(key), before)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(self.host.audits(identity, "runtime-alert-resolved"), [])

    def test_H13_foreign_history_cannot_confirm_batch_or_create_another_main_turn(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        before = copy.deepcopy(self.host.view(key))
        attempt = self.host.attempt(key, "D")
        event = {"type": "input/history", "client_message_id": "OWN foreign historical client",
                 "thread_id": self.host.root_id(key), "turn_id": attempt["expected_turn_id"],
                 "item_id": "OWN stale item", "input": self.host.delivery(key, "D")["input"],
                 "scan": {"thread_id": self.host.root_id(key), "turn_id": None, "sort_direction": "asc",
                          "from_cursor": None, "final_cursor": None, "complete": True}}
        self.assertIs(self.host.client.event(self.host.binding(key), event), False)
        self.host.present(key, notifier)
        after = self.host.view(key)
        self.assertEqual(self.host.delivery(key, "D")["status"], "pending")
        self.assertEqual(self.host.alert(key, identity)["status"], "pending")
        self.assertEqual(after["inputs"][attempt["client_message_id"]], before["inputs"][attempt["client_message_id"]])
        self.assertEqual(after["main"]["seen_turns"], before["main"]["seen_turns"])
        self.assertEqual(after["main"]["completed_turns"], before["main"]["completed_turns"])
        self.assertEqual(len(self.host.audits(identity, "runtime-alert-pending")), 1)
        self.assertEqual(self.host.audits(identity, "runtime-alert-resolved"), [])

    def test_H14_known_rejection_cannot_claim_or_announce_delivery(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        self.host.reject(key)
        self.assertEqual(self.host.alert(key, identity)["status"], "resolved")
        self.assertEqual(self.host.delivery(key, "D")["status"], "pending")
        self.assertEqual(self.host.attempt(key, "D")["status"], "rejected")
        self.assertIs(self.host.client.event(self.host.binding(key),
                      self.host.claim_event(key, identity, "receipt-resolved")), False)
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(self.host.audits(identity, "runtime-alert-resolved"), [])
        self.assertNotIn("receipt_resolution_claim_revision", self.host.alert(key, identity).get("presentation", {}))

    def test_H15_safe_reopened_uncertainty_reuses_condition_and_initial_claim(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        initial = self.assert_claim(key, identity)
        self.host.reject(key)
        self.host.send_attempt(key, "D", "2")
        self.host.uncertainty(key, "D", "OWN second attempt receipt uncertain", "2")
        self.host.present(key, notifier)
        self.assertEqual(self.host.alert(key, identity)["status"], "pending")
        self.assertEqual(self.assert_claim(key, identity), initial)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(len(self.host.delivery(key, "D")["attempts"]), 2)

    def test_H16_two_batches_get_independent_pending_claims(self):
        key, first = self.pending()
        second = self.host.uncertain(key, "E")
        notifier = self.host.present(key)
        self.assertEqual(len(notifier.calls), 2)
        self.assert_claim(key, first)
        self.assert_claim(key, second)
        self.assertEqual(len(self.host.audits(first, "runtime-alert-pending")), 1)
        self.assertEqual(len(self.host.audits(second, "runtime-alert-pending")), 1)
        self.host.ack(key)
        self.host.present(key, notifier)
        self.assertEqual(self.host.alert(key, second)["status"], "pending")
        self.assertEqual(self.host.delivery(key, "E")["status"], "pending")

    def test_H17_identical_batch_string_is_independent_across_tasks(self):
        first, identity = self.pending(name="first", issue=1)
        second, _ = self.pending(name="second", issue=1)
        a, b = self.host.present(first), self.host.present(second)
        before_b = copy.deepcopy(self.host.view(second))
        self.host.ack(first)
        self.host.present(first, a)
        self.host.present(second, b)
        self.assertEqual(self.host.view(second), before_b)
        self.assertEqual(len(a.calls), 1)
        self.assertEqual(len(b.calls), 1)
        records = self.host.audits(identity, "runtime-alert-pending")
        self.assertEqual({record["target"] for record in records}, {"first", "second"})
        self.assertEqual(len(records), 2)

    def replacement(self, *, ticket=1):
        key, identity = self.pending()
        notifier = self.host.present(key)
        old = copy.deepcopy(self.host.binding(key))
        old_attempt = copy.deepcopy(self.host.attempt(key, "D"))
        self.host.task(key[0], key[1], ticket=ticket)
        new_identity = self.host.uncertain(key, "D")
        self.assertNotEqual(self.host.binding(key)["launch_id"], old["launch_id"])
        self.assertEqual(self.host.binding(key)["conversation_id"], old["conversation_id"])
        new_before = copy.deepcopy(self.host.view(key))
        stale_claim = self.host.claim_event(key, identity, revision=new_before["revision"])
        self.assertIs(self.host.client.event(old, stale_claim), False)
        self.assertIs(self.host.client.event(old, {"type": "delivery/ack", "batch_id": "D",
            "attempt_id": old_attempt["attempt_id"], "client_message_id": old_attempt["client_message_id"],
            "thread_id": old["conversation_id"], "turn_id": old_attempt["expected_turn_id"]}), False)
        self.assertEqual(self.host.view(key), new_before)
        self.host.present(key, notifier)
        self.assertEqual(len(notifier.calls), 2)
        self.assert_claim(key, new_identity)
        records = self.host.audits(identity, "runtime-alert-pending")
        self.assertEqual(len(records), 2)
        self.assert_original_binding(records[0], old)
        self.assert_original_binding(records[1], self.host.binding(key))

    def test_H18_fresh_physical_launch_rejects_old_callbacks(self):
        self.replacement()

    def test_H19_new_implement_ticket_with_same_conversation_has_new_identity(self):
        self.replacement(ticket=2)

    def test_H21_forced_retirement_before_claim_suppresses_presentation_and_claim(self):
        key, identity = self.pending()
        binding = copy.deepcopy(self.host.binding(key))
        revision = self.host.view(key)["revision"]
        self.assertEqual(self.host.client.retire(binding, revision, reason="forced"), "retired")
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(binding, self.host.claim_event(key, identity)), False)
        self.assertEqual(self.host.view(key), before)
        notifier = self.host.present(key)
        self.assert_no_effect(identity, notifier)
        self.assertNotIn("presentation", self.host.alert(key, identity))

    def test_H21_replacement_before_first_claim_cannot_present_retained_old_condition(self):
        key, identity = self.pending()
        old = copy.deepcopy(self.host.binding(key))
        old_file = self.host.snapshot_path(key)
        self.host.task(*key)
        self.assertNotEqual(self.host.binding(key)["launch_id"], old["launch_id"])
        before = copy.deepcopy(self.host.view(key))
        event = {"type": "alert/presentation-claimed", "observed_revision": before["revision"],
                 "alert_identity": identity, "phase": "pending"}
        self.assertIs(self.host.client.event(old, event), False)
        self.assertEqual(self.host.view(key), before)
        notifier = self.host.present(key)
        self.assert_no_effect(identity, notifier)
        retained = json.loads(old_file.read_text())
        old_alert, = [item for item in retained["alerts"] if item.get("batch_id") == "D"]
        self.assertNotIn("presentation", old_alert)
        new_identity = self.host.compatible(key, "OWN new physical launch problem")
        self.host.present(key, notifier)
        self.assert_pending_effect(key, new_identity, notifier)
        self.assertEqual(self.host.audits(identity), [])

    def test_H21_missing_current_association_cannot_use_retained_launch_as_authority(self):
        key, identity = self.pending()
        pointer = self.host.snapshot_path(key).parent / "current.json"
        original = pointer.read_bytes()
        snapshot = self.host.snapshot_path(key)
        retained_before = json.loads(snapshot.read_text())
        original_binding = copy.deepcopy(self.host.binding(key))
        self.host.listener.close()
        pointer.unlink()
        self.host.listener.start()
        observed = self.host.view(key)
        self.assertTrue(observed is None or isinstance(observed, dict), "public view is null or conservative managed evidence")
        self.assertFalse(pointer.exists(), "fixture must remove actual current association")
        self.assertNotEqual(observed, retained_before,
                            "missing association must not return retained history as a current valid snapshot")
        self.assertIs(self.host.client.event(original_binding, {
            "type": "alert/presentation-claimed", "observed_revision": retained_before["revision"],
            "alert_identity": identity, "phase": "pending"}), False)
        notifier = self.host.present(key)
        self.assert_no_effect(identity, notifier)
        retained = json.loads(snapshot.read_text())
        self.assertEqual(retained, retained_before, "lost association cannot mutate/select retained launch history")
        old_alert, = [item for item in retained["alerts"] if item.get("batch_id") == "D"]
        self.assertNotIn("presentation", old_alert)
        self.host.listener.close()
        pointer.write_bytes(original)  # Restore exact owned pointer, never forge evidence.
        self.host.listener.start()
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)

    def test_H22_known_park_after_claim_before_effect_consumes_claim_without_effect(self):
        key, identity = self.pending()
        self.host.listener.proxy.after_new_claim = lambda _: self.host.save(key, park="known-external-park")
        notifier = self.host.present(key)
        self.assert_claim(key, identity)
        self.assert_no_effect(identity, notifier)
        self.host.save(key, park="")
        self.host.present(key, notifier)
        self.assert_no_effect(identity, notifier)

    def test_H23_known_park_during_real_audit_open_suppresses_later_push(self):
        key, identity = self.pending()
        process = self.host.present_process(key, audit_action="park")
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key).park, "fixture-known-park")
        records = self.host.audits(identity, "runtime-alert-pending")
        self.assertEqual(len(records), 1, "real audit append was the earlier independently checked effect")
        self.assertEqual(self.host.process_calls(), [], "known current park must suppress separate notifier effect")
        self.assert_claim(key, identity)

    def test_H24_own_receipt_between_claim_and_effect_suppresses_stale_pending_push(self):
        key, identity = self.pending()
        self.host.listener.proxy.after_new_claim = lambda _: self.host.ack(key)
        notifier = self.host.present(key)
        self.assert_claim(key, identity)
        self.assertEqual(notifier.calls, [])
        self.assertEqual(self.host.audits(identity, "runtime-alert-pending"), [])
        self.assertEqual(self.host.alert(key, identity)["status"], "resolved")

    def test_H25_replacement_racing_after_final_check_never_relabels_context(self):
        key, identity = self.pending()
        original = copy.deepcopy(self.host.binding(key))
        replacement = []
        def replace_during_external_send():
            self.host.task(*key, ticket=2)
            replacement.append(self.host.binding(key))
        notifier = RecordingNotifier(before_return=replace_during_external_send)
        self.host.present(key, notifier)
        self.assertEqual(len(notifier.calls), 1)
        note = notifier.calls[0]["context"]["note"]
        self.assertIn(original["launch_id"], note)
        self.assertNotIn(replacement[0]["launch_id"], note)
        records = self.host.audits(identity, "runtime-alert-pending")
        self.assertEqual(len(records), 1)
        self.assert_original_binding(records[0], original)

    def test_H26_compatibility_unknown_visibility_preserves_shape_and_claim(self):
        key, identity = self.compatibility()
        before = self.host.view(key)
        self.assertEqual(before["main"]["status"], "unknown")
        self.assertEqual(before["inventory"], "unknown")
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        self.assertNotIn("status", self.host.alert(key, identity))
        self.assertEqual(without_presentation(self.host.view(key)), without_presentation(before))
        admitted = copy.deepcopy(self.host.alert(key, identity))
        self.host.listener.restart()
        self.host.present(key, notifier)
        self.assertEqual(self.host.alert(key, identity), admitted)
        self.assertEqual(len(notifier.calls), 1)

    def test_H27_exact_compatibility_message_dedups_distinct_message_is_independent(self):
        key, identity = self.compatibility()
        notifier = self.host.present(key)
        self.host.compatible(key, identity["message"], repeated=True)
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)
        second = self.host.compatible(key, identity["message"] + " distinct")
        self.host.present(key, notifier)
        self.assertEqual(len(notifier.calls), 2)
        self.assert_claim(key, second)
        self.assertEqual(len(self.host.audits(second, "runtime-alert-pending")), 1)

    def test_H28_delivery_receipt_does_not_resolve_compatibility(self):
        key, delivery = self.pending()
        compatible = self.host.compatible(key, "separate compatibility condition")
        notifier = self.host.present(key)
        before = copy.deepcopy(self.host.alert(key, compatible))
        self.host.ack(key)
        self.host.present(key, notifier)
        self.assertEqual(self.host.alert(key, compatible), before)
        self.assertIs(self.host.client.event(self.host.binding(key),
                      self.host.claim_event(key, compatible, "receipt-resolved")), False)
        self.assertEqual(self.host.audits(compatible, "runtime-alert-resolved"), [])
        self.assertEqual(self.host.alert(key, delivery)["status"], "resolved")

    def test_H28_delivery_rejection_cannot_clear_compatibility_or_claim_its_resolution(self):
        key, delivery = self.pending()
        compatible = self.host.compatible(key, "OWN compatibility independent of rejection")
        notifier = self.host.present(key)
        before = copy.deepcopy(self.host.alert(key, compatible))
        self.host.reject(key)
        self.host.present(key, notifier)
        self.assertEqual(self.host.alert(key, compatible), before)
        self.assertEqual(len(notifier.calls), 2)
        self.assertIs(self.host.client.event(self.host.binding(key),
                      self.host.claim_event(key, compatible, "receipt-resolved")), False)
        self.assertEqual(self.host.audits(compatible, "runtime-alert-resolved"), [])
        self.assertEqual(self.host.delivery(key, "D")["status"], "pending")
        self.assertEqual(self.host.alert(key, delivery)["status"], "resolved")

    def test_H29_prior_null_root_claim_survives_refinement_without_repeat(self):
        key, identity = self.compatibility(unbound=True)
        original = copy.deepcopy(self.host.binding(key))
        self.assertIsNone(original["conversation_id"])
        notifier = self.host.present(key)
        before = copy.deepcopy(self.host.alert(key, identity))
        self.host.refine(key)
        self.host.present(key, notifier)
        self.assertEqual(self.host.alert(key, identity), before)
        self.assert_pending_effect(key, identity, notifier)
        self.assert_original_binding(self.host.audits(identity)[0], original)

    def test_H30_refinement_after_admitted_claim_keeps_original_null_attribution(self):
        key, identity = self.compatibility(unbound=True)
        original = copy.deepcopy(self.host.binding(key))
        self.host.listener.proxy.after_new_claim = lambda _: self.host.refine(key)
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        self.assertIsNotNone(self.host.binding(key)["conversation_id"])
        self.assert_original_binding(self.host.audits(identity)[0], original)
        self.assertNotIn(self.host.root_id(key), notifier.calls[0]["context"]["note"],
                         "new learned root must not replace admitted null attribution")

    def test_H31_refined_binding_authorizes_fresh_claim_old_null_does_not(self):
        key, old_identity = self.compatibility(unbound=True)
        old = copy.deepcopy(self.host.binding(key))
        self.host.present(key)
        self.host.refine(key)
        repeated = self.host.claim_event(key, old_identity)
        self.assertIs(self.host.client.event(self.host.binding(key), repeated), False)
        new_identity = self.host.compatible(key, "new problem after exact-root refinement")
        event = self.host.claim_event(key, new_identity)
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(old, event), False)
        self.assertEqual(self.host.view(key), before)
        self.assertIs(self.host.client.event(self.host.binding(key), event), True)
        self.assert_claim(key, new_identity)

    def test_H35_exact_container_claim_is_403_and_cannot_consume_notice(self):
        key, identity = self.pending()
        bound = self.host.http.BoundClient(self.host.state_dir, *key, self.host.binding(key)["launch_id"])
        before = copy.deepcopy(self.host.view(key))
        event = self.host.claim_event(key, identity)
        with self.assertRaises(RuntimeError):
            bound.event(self.host.binding(key), event)
        request = self.host.listener.proxy.records[-1]
        self.assertFalse(request["host_authenticated"])
        self.assertEqual(request["status"], 403)
        self.assertEqual(self.host.view(key), before)
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        host_claims = [r for r in self.host.listener.proxy.records if
                       r["body"].get("event", {}).get("type") == "alert/presentation-claimed" and r["value"] is True]
        self.assertEqual(len(host_claims), 1)
        self.assertTrue(host_claims[0]["host_authenticated"])
        token = (self.host.state_dir / "runtime-host-token").read_text().strip()
        self.assertNotIn(token, json.dumps(self.host.view(key)))
        self.assertNotIn(token, json.dumps(host_claims[0]["body"]))

    def test_H35_raw_no_header_claim_is_403_without_mutation(self):
        key, identity = self.pending()
        before = copy.deepcopy(self.host.view(key))
        status, _ = wire_request(self.host.listener.path, "/runtime/event",
                                 {"binding": self.host.binding(key), "event": self.host.claim_event(key, identity)})
        self.assertEqual(status, 403)
        self.assertEqual(self.host.view(key), before)
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)

    def test_H36_concurrent_host_claims_have_one_newly_durable_true(self):
        key, identity = self.pending()
        event = self.host.claim_event(key, identity)
        binding = self.host.binding(key)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(self.host.client.event, binding, event) for _ in range(2)]
            responses = [future.result(timeout=5) for future in calls]
        self.assertEqual(sorted(responses), [False, True])
        claim = self.assert_claim(key, identity)
        notifier = self.host.present(key)
        self.assert_no_effect(identity, notifier)
        self.assertEqual(self.assert_claim(key, identity), claim)

    def test_H36_already_claimed_phase_returns_false_with_current_revision(self):
        key, identity = self.pending()
        self.assertIs(self.host.client.event(self.host.binding(key), self.host.claim_event(key, identity)), True)
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(self.host.binding(key), self.host.claim_event(key, identity)), False)
        self.assertEqual(self.host.view(key), before)

    def test_H36_stale_revision_has_no_claim_and_fresh_revision_can_claim(self):
        key, identity = self.pending()
        old_revision = self.host.view(key)["revision"]
        self.host.uncertainty(key, "D", "changed diagnostic advances causal revision")
        before = copy.deepcopy(self.host.view(key))
        self.assertGreater(before["revision"], old_revision)
        self.assertIs(self.host.client.event(self.host.binding(key),
                      self.host.claim_event(key, identity, revision=old_revision)), False)
        self.assertEqual(self.host.view(key), before)
        self.assertIs(self.host.client.event(self.host.binding(key), self.host.claim_event(key, identity)), True)
        self.assert_claim(key, identity)

    def test_H36_foreign_binding_has_no_claim_and_current_binding_can_claim(self):
        key, identity = self.pending()
        foreign = {**self.host.binding(key), "issue": 999}
        before = copy.deepcopy(self.host.view(key))
        event = self.host.claim_event(key, identity)
        self.assertIs(self.host.client.event(foreign, event), False)
        self.assertEqual(self.host.view(key), before)
        self.assertIs(self.host.client.event(self.host.binding(key), event), True)
        self.assert_claim(key, identity)

    def test_H36_receipt_resolution_phase_requires_own_confirmed_receipt(self):
        key, identity = self.pending()
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(self.host.binding(key),
                      self.host.claim_event(key, identity, "receipt-resolved")), False)
        self.assertEqual(self.host.view(key), before)
        self.host.ack(key)
        self.assertIs(self.host.client.event(self.host.binding(key),
                      self.host.claim_event(key, identity, "receipt-resolved")), True)
        self.assert_claim(key, identity, "receipt_resolution_claim_revision")

    def test_H36_retired_launch_denies_fresh_revision_claim_while_owned_control_can_claim(self):
        key, identity = self.pending()
        binding = copy.deepcopy(self.host.binding(key))
        snapshot = self.host.snapshot_path(key)
        self.assertEqual(self.host.client.retire(binding, self.host.view(key)["revision"], reason="forced"), "retired")
        before = json.loads(snapshot.read_text())
        event = {"type": "alert/presentation-claimed", "observed_revision": before["revision"],
                 "alert_identity": identity, "phase": "pending"}
        self.assertIs(self.host.client.event(binding, event), False)
        self.assertEqual(json.loads(snapshot.read_text()), before)
        control = self.host.task("eligible-control", 2)
        positive = self.host.uncertain(control)
        self.assertIs(self.host.client.event(self.host.binding(control), self.host.claim_event(control, positive)), True)
        self.assert_claim(control, positive)

    def test_H36_resolved_condition_cannot_claim_pending_phase(self):
        key, identity = self.pending()
        self.host.ack(key)
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(self.host.binding(key), self.host.claim_event(key, identity)), False)
        self.assertEqual(self.host.view(key), before)
        self.assertIs(self.host.client.event(self.host.binding(key),
                      self.host.claim_event(key, identity, "receipt-resolved")), True)
        self.assert_claim(key, identity, "receipt_resolution_claim_revision")

    def test_H37_dry_run_has_no_claim_history_notice_or_queue_change(self):
        key, identity = self.pending()
        self.host.messages.append(self.host.state_dir, *key, "OWN queued input", "fixture-operator")
        before = copy.deepcopy(self.host.view(key))
        queue = self.host.messages.all_messages(self.host.state_dir, *key)
        task = self.host.state.load(self.host.state_dir, *key)
        notifier = self.host.present(key, dry_run=True)
        self.assert_no_effect(identity, notifier)
        self.assertEqual(self.host.view(key), before)
        self.assertEqual(self.host.messages.all_messages(self.host.state_dir, *key), queue)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), task)
        # Positive contrast: same saved task/condition presents when dry_run is false.
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)

    def test_H38_pending_audit_has_typed_identity_diagnostic_and_original_binding(self):
        key, identity = self.pending()
        original = copy.deepcopy(self.host.binding(key))
        attempt = self.host.attempt(key, "D")["attempt_id"]
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        record = self.host.audits(identity)[0]
        self.assertEqual(set(record), {"ts", "event", "target", "issue", "stage", "model", "actor", "detail"})
        detail = json.loads(record["detail"])
        self.assert_original_binding(record, original)
        self.assertTrue(identity_in_detail(detail, identity))
        leaves = list(values(detail))
        self.assertIn("pending", leaves)
        self.assertIn("OWN receipt remains uncertain", leaves)
        self.assertIn("D", leaves)
        self.assertIn(attempt, leaves)

    def test_H38_resolved_audit_is_bound_to_own_confirmed_receipt(self):
        key, identity = self.pending()
        original = copy.deepcopy(self.host.binding(key))
        self.host.present(key)
        self.host.history_receipt(key)
        self.host.present(key)
        record, = self.host.audits(identity, "runtime-alert-resolved")
        detail = json.loads(record["detail"])
        self.assert_original_binding(record, original)
        self.assertTrue(identity_in_detail(detail, identity))
        self.assertIn("receipt-resolved", list(values(detail)))
        self.assertEqual(self.host.delivery(key, "D")["status"], "confirmed")
        self.assertEqual(self.host.attempt(key, "D")["receipt"]["source"], "history")
        # JSON member spelling is finalized by publication; typed batch/binding/phase
        # identity is asserted without inventing an author's representation policy.
        receipt = self.host.attempt(key, "D")["receipt"]
        structured = [node for node in values(detail) if isinstance(node, dict)]
        complete_receipt = any(all(node.get(k) == v for k, v in receipt.items()) for node in structured)
        # Both a nested typed receipt and a semantic boolean confirmation field are
        # legitimate JSON representations; do not mandate a particular member spelling.
        semantic_confirmation = any(value is True and "receipt" in name.lower() and
                                    any(term in name.lower() for term in ["confirm", "accept"])
                                    for node in structured for name, value in node.items())
        self.assertTrue(complete_receipt or semantic_confirmation,
                        "resolved JSON must explicitly attribute confirmation to its own accepted receipt")

    def test_H38_compatibility_audit_retains_kind_message_without_receipt_resolution(self):
        key, identity = self.compatibility()
        self.host.present(key)
        record, = self.host.audits(identity)
        detail = json.loads(record["detail"])
        self.assertTrue(identity_in_detail(detail, identity))
        self.assertIn(identity["message"], list(values(detail)))
        self.assertNotIn("receipt-resolved", list(values(detail)))

    def test_H39_delivery_context_and_rendering_show_uncertainty_without_false_authority(self):
        key, identity = self.pending()
        notifier = self.host.present(key)
        ctx = notifier.calls[0]["context"]
        self.assertEqual(set(ctx), {"issue", "title", "url", "note", "target"})
        self.assertEqual(ctx["url"], "https://github.com/fixture-owner/fixture-repo/issues/1")
        self.assertEqual((ctx["issue"], ctx["title"], ctx["target"]),
                         (key[1], self.host.tasks[key].title, key[0]))
        for exact in [self.host.binding(key)["launch_id"], "implement", "1", "delivery-uncertain", "D",
                      "OWN receipt remains uncertain"]:
            self.assertIn(exact, ctx["note"])
        note = ctx["note"].lower()
        for concepts in [("uncertain",), ("resend", "held"), ("history", "reconcil")]:
            self.assertTrue(all(concept in note for concept in concepts))
        templates = importlib.import_module("telegram.templates")
        rendered = templates.render("runtime_alert", **ctx)
        self.assertIn(self.host.tasks[key].title, rendered)
        self.assertIn(self.host.binding(key)["launch_id"], rendered)
        for misleading in ["task failed", "task parked", "capacity freed", "worker succeeded", "reply to continue"]:
            self.assertNotIn(misleading, rendered.lower())

    def test_H39_compatibility_rendering_does_not_invent_delivery_uncertainty(self):
        key, identity = self.compatibility(message="OWN malformed lifecycle record")
        notifier = self.host.present(key)
        ctx = notifier.calls[0]["context"]
        self.assertIn("compatibility", ctx["note"])
        self.assertIn(identity["message"], ctx["note"])
        self.assertNotIn("delivery-uncertain", ctx["note"])
        self.assertNotIn("automatic resend", ctx["note"].lower())
        rendered = importlib.import_module("telegram.templates").render("runtime_alert", **ctx)
        self.assertIn(identity["message"], rendered)

    def test_H40_presentation_preserves_real_task_queue_receipts_workers_and_clock(self):
        key, identity = self.pending()
        self.host.messages.append(self.host.state_dir, *key, "first queued OWN input", "fixture")
        self.host.messages.append(self.host.state_dir, *key, "second queued OWN input", "fixture")
        operator = "operator-held-" + self.host.binding(key)["launch_id"]
        self.assertIs(self.host.client.accept_input(self.host.binding(key), operator), True)
        task = self.host.state.load(self.host.state_dir, *key)
        queue = self.host.messages.all_messages(self.host.state_dir, *key)
        before = self.host.view(key)
        self.assertTrue(before["inputs"])
        self.assertTrue(before["workers"])
        self.assertTrue(before["completions"])
        self.assertIsNotNone(before["wait"], "setup must exercise a real retained wait clock")
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), task)
        self.assertEqual(self.host.messages.all_messages(self.host.state_dir, *key), queue)
        self.assertEqual(without_presentation(self.host.view(key)), without_presentation(before))

    def test_H41_public_direct_entrypoint_has_no_unrelated_pass_dependency(self):
        key, identity = self.pending()
        # No GitHub/session/usage/inbound/artifact object is constructed for this direct call.
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), self.host.tasks[key])

    def test_H41_real_pass_reuses_public_entrypoint_before_normal_task_drive(self):
        key, identity = self.pending()
        self.host.signal(key, "blocked", note="OWN explicit stage help")
        result = self.host.run_pass()
        entries = [i for i, event in enumerate(result["trace"]) if event == ["presentation-entry"]]
        notices = [i for i, event in enumerate(result["trace"]) if event == ["notice", "runtime_alert", *key]]
        ends = [i for i, event in enumerate(result["trace"]) if event == ["end", *key]]
        self.assertTrue(entries, "real in-flight composition must call the supported presenter")
        self.assertEqual(len(notices), 1)
        self.assertEqual(len(ends), 1, "explicit blocked signal must still reach ordinary task drive")
        self.assertLess(entries[0], notices[0])
        self.assertLess(notices[0], ends[0])
        self.assert_claim(key, identity)
        self.assertEqual(len(self.host.audits(identity, "runtime-alert-pending")), 1)
        self.assertEqual(result["sent"], [], "presentation must not enter agent-input routing")

    def test_H42_background_clock_exact_cap_holds_and_strictly_greater_retires(self):
        key = self.host.task()
        identity = self.host.compatible(key, "OWN background cap diagnostic")
        self.host.background(key)
        since = self.host.view(key)["wait"]["since"]
        revision = self.host.view(key)["revision"]
        before = copy.deepcopy(self.host.view(key))
        self.assertEqual(self.host.client.retire(self.host.binding(key), revision,
                         reason="background", cap=10800, now=since + 10800), "held")
        self.assertEqual(self.host.view(key), before)
        self.assertEqual(self.host.client.retire(self.host.binding(key), revision,
                         reason="background", cap=10800, now=since + 10800.001), "retired")
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), self.host.tasks[key],
                         "runtime fence never takes dispatcher TaskState ownership")

    def test_H42_foreground_main_remains_exempt_from_old_background_cap(self):
        key = self.host.task()
        identity = self.host.compatible(key, "OWN foreground exemption diagnostic")
        self.host.background(key, now=time.time() - 10801)
        self.host.apply(key, {"type": "turn/started", "thread_id": self.host.root_id(key),
                              "turn_id": "OWN active foreground after background"})
        before = copy.deepcopy(self.host.view(key))
        self.assertEqual(self.host.client.retire(self.host.binding(key), before["revision"],
                         reason="background", cap=10800, now=time.time()), "held")
        result = self.host.run_pass()
        self.assertEqual(result["ended"], [])
        self.assertEqual(result["spawned"], [])
        self.assertEqual(self.host.state.load(self.host.state_dir, *key).park, "")
        self.assertEqual(self.host.view(key)["main"], before["main"])
        self.assertEqual(self.host.view(key)["wait"], before["wait"])

    def test_H42_actual_pass_preserves_existing_180_minute_cap_note(self):
        baseline = self.host.task("baseline", 1)
        alerted = self.host.task("alerted", 2)
        identity = self.host.compatible(alerted, "OWN cap-note preservation diagnostic")
        now = time.time() - 10801
        self.host.background(baseline, now=now)
        self.host.background(alerted, now=now)
        result = self.host.run_pass(capacity=2)
        a = self.host.state.load(self.host.state_dir, *baseline)
        b = self.host.state.load(self.host.state_dir, *alerted)
        self.assertTrue(a.park)
        self.assertEqual((b.park, b.park_note), (a.park, a.park_note),
                         "alert presentation must preserve the ordinary cap reason and note")
        expected_note = "(background work still running after 180m — cap reached)"
        self.assertEqual(a.park_note, expected_note)
        self.assertEqual(b.park_note, expected_note)
        self.assertTrue(any(call["context"].get("target") == alerted[0] and
                            call["context"].get("issue") == alerted[1] and
                            call["context"].get("note") == expected_note + "\n\ngeneric OWN recent terminal output"
                            for call in result["calls"]),
                        "existing operator notifier context must retain the exact default cap note")
        self.assertEqual({tuple(key) for key in result["ended"]}, {baseline, alerted})

    def test_H42_current_background_wait_holds_capacity_and_queued_input(self):
        key = self.host.task()
        identity = self.host.compatible(key, "OWN capacity retention diagnostic")
        self.host.background(key)
        self.host.messages.append(self.host.state_dir, *key, "OWN queued input remains independent", "fixture")
        queue = self.host.messages.all_messages(self.host.state_dir, *key)
        candidate = {"number": 99, "title": "OWN capacity candidate",
                     "url": "https://github.com/fixture-owner/fixture-repo/issues/99", "effort": 1,
                     "labels": ["track:fixture"]}
        result = self.host.run_pass(candidates={key[0]: [candidate]}, capacity=1, usage_available=True)
        self.assertEqual(result["ended"], [])
        self.assertEqual(result["spawned"], [])
        self.assertEqual(result["sent"], [], "queue must not be drained as alert/result input")
        self.assertFalse(any(call["method"] == "claim" for call in result["github"]))
        self.assertEqual(self.host.messages.all_messages(self.host.state_dir, *key), queue)
        self.assertIsNone(self.host.state.load(self.host.state_dir, key[0], 99))
        self.assertEqual(self.host.state.load(self.host.state_dir, *key).park, "")

    def test_H42_explicit_stage_help_still_parks_with_its_note_over_running_work(self):
        key = self.host.task()
        identity = self.host.compatible(key, "OWN stage-help diagnostic")
        self.host.background(key)
        original = copy.deepcopy(self.host.binding(key))
        self.host.signal(key, "blocked", note="gate cannot run")
        result = self.host.run_pass()
        task = self.host.state.load(self.host.state_dir, *key)
        self.assertTrue(task.park)
        self.assertIn("gate cannot run", task.park_note)
        self.assertEqual(result["ended"], [list(key)])
        self.assertTrue(any("gate cannot run" in call["context"].get("note", "") for call in result["calls"]))
        self.assertIs(self.host.client.event(original, {"type": "turn/started",
                      "thread_id": original["conversation_id"], "turn_id": "late-ended-launch"}), False)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), task)

    def test_H42_explicit_ci_wait_retains_run_and_closure_authority(self):
        key = self.host.task()
        identity = self.host.compatible(key, "OWN CI-wait diagnostic")
        self.host.background(key)
        original = copy.deepcopy(self.host.binding(key))
        self.host.signal(key, "awaiting-ci", run_id=343, note="OWN CI still running")
        result = self.host.run_pass(run_status="")
        task = self.host.state.load(self.host.state_dir, *key)
        self.assertTrue(task.park)
        self.assertEqual(task.ci_run_id, 343)
        self.assertEqual(result["ended"], [list(key)])
        self.assertIs(self.host.client.event(original, {"type": "turn/started", "thread_id": original["conversation_id"],
                      "turn_id": "late-after-ci"}), False)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), task)

    def test_H42_spec_review_request_preserves_artifact_and_operator_authority(self):
        key = self.host.task(stage="SPEC")
        identity = self.host.compatible(key, "OWN spec-review diagnostic")
        self.host.background(key)
        spec_path = Path(self.host.tasks[key].worktree) / ".agent" / "spec.md"
        spec_path.write_text("# Generic OWN spec\nNo private project data.\n")
        self.host.signal(key, "awaiting-review", artifact=".agent/spec.md", note="OWN spec ready")
        self.host.owned_origin(key)
        result = self.host.run_pass()
        task = self.host.state.load(self.host.state_dir, *key)
        self.assertEqual(task.stage, self.host.state.Stage.AWAITING_SPEC_REVIEW)
        self.assertEqual(task.spec_path, ".agent/spec.md")
        self.assertEqual(task.operator_request.kind, "spec-approval")
        self.assertEqual(result["ended"], [], "pre-expiry review grace preserves the existing session")
        self.assertEqual(result["spawned"], [])

    def test_H42_answers_request_keeps_its_existing_artifact_and_closes_background_launch(self):
        key = self.host.task()
        identity = self.host.compatible(key, "OWN answers-request diagnostic")
        self.host.background(key)
        artifact = Path(self.host.tasks[key].worktree) / ".agent" / "answers.md"
        artifact.write_text("# Generic OWN questions\n- Select a generic fixture choice.\n")
        self.host.signal(key, "awaiting-answers", artifact=".agent/answers.md", note="OWN clarification required")
        self.host.owned_origin(key)
        result = self.host.run_pass()
        task = self.host.state.load(self.host.state_dir, *key)
        self.assertTrue(task.park)
        self.assertEqual(task.operator_request.kind, "answers")
        self.assertEqual(task.operator_request.path, ".agent/answers.md")
        self.assertEqual(result["ended"], [list(key)])
        self.assertEqual(result["spawned"], [])

    def test_H42_merged_pr_retains_existing_terminal_task_transition(self):
        key = self.host.task(stage="REVIEW")
        identity = self.host.compatible(key, "OWN finished-review diagnostic")
        binding = copy.deepcopy(self.host.binding(key))
        self.assertEqual(self.host.client.retire(binding, self.host.view(key)["revision"], reason="forced"), "retired")
        self.host.save(key, stage=self.host.state.Stage.PR_OPEN, pr_number=55)
        result = self.host.run_pass(pr_state="MERGED", pr_branch=self.host.tasks[key].branch, dead=[list(key)])
        task = self.host.state.load(self.host.state_dir, *key)
        self.assertEqual(task.stage, self.host.state.Stage.DONE)
        self.assertTrue(task.done_at or task.terminal_at)
        self.assertEqual(result["spawned"], [])
        self.assertEqual(result["resumed"], [])

    def test_H42_delivered_failure_does_not_reset_exhausted_gate_loop(self):
        baseline = self.host.task("baseline", 1)
        self.host.propose_result(baseline, outcome_status="failed")
        self.host.ack(baseline)
        prior_turn = self.host.view(baseline)["main"]["turn_id"]
        self.host.apply(baseline, {"type": "turn/completed", "thread_id": self.host.root_id(baseline),
                                "turn_id": prior_turn, "status": "completed"})
        self.host.save(baseline, gate_rounds=2)
        self.host.signal(baseline, "blocked", loop="gate", round=3, note="OWN gate fix remains exhausted")
        key = self.host.task("alerted", 2)
        identity = self.host.uncertain(key, outcome_status="failed")
        self.host.ack(key)
        active = self.host.view(key)["main"]["turn_id"]
        self.host.apply(key, {"type": "turn/completed", "thread_id": self.host.root_id(key),
                              "turn_id": active, "status": "completed"})
        self.host.save(key, gate_rounds=2)
        self.host.signal(key, "blocked", loop="gate", round=3, note="OWN gate fix remains exhausted")
        result = self.host.run_pass(capacity=2)
        task = self.host.state.load(self.host.state_dir, *key)
        original_policy = self.host.state.load(self.host.state_dir, *baseline)
        self.assertTrue(task.park)
        self.assertEqual((task.park, task.gate_rounds), (original_policy.park, original_policy.gate_rounds))
        self.assertGreaterEqual(task.gate_rounds, 2, "a delivered error/presentation cannot reset prior fix allowance")
        self.assertEqual(result["spawned"], [])
        self.assertEqual({tuple(item) for item in result["ended"]}, {baseline, key})
        ordinary = [call for call in result["calls"] if call["template"] != "runtime_alert"]
        self.assertTrue(ordinary, "existing exhausted-loop operator notification remains required")
        self.assertTrue(any("gate" in call["context"].get("note", "").lower() and
                            any(word in call["context"].get("note", "").lower() for word in ["cap", "limit", "exhaust"])
                            for call in ordinary))
        self.assertEqual(self.host.delivery(key, "D")["status"], "confirmed")
        self.assertEqual(self.host.alert(key, identity)["status"], "resolved")

    def test_H42_done_ticket_closes_old_launch_and_cannot_be_revived_by_late_outcome(self):
        key = self.host.task(ticket=1)
        identity = self.host.compatible(key, "OWN done-ticket closure diagnostic")
        worker_id = self.host.background(key)
        original = copy.deepcopy(self.host.binding(key))
        self.host.save(key, ticket_count=1)
        self.host.signal(key, "done", note="OWN implementation ticket complete")
        result = self.host.run_pass(usage_available=True)
        task = self.host.state.load(self.host.state_dir, *key)
        self.assertIn(list(key), result["ended"])
        self.assertNotEqual((task.stage.value, task.ticket_cursor), ("implement", 1),
                            "done must retain the existing next-stage/admission transition")
        observation = {"identity": worker_id, "ancestry": [
            {"thread_id": worker_id["thread_id"], "parent_thread_id": original["conversation_id"],
             "source_kind": "thread-spawn", "source_parent_thread_id": original["conversation_id"], "depth": 1},
            {"thread_id": original["conversation_id"], "parent_thread_id": None,
             "source_kind": "bound-root", "source_parent_thread_id": None, "depth": 0}],
            "thread_status": "idle", "active_flags": [], "status": "completed", "outcome": {
                "status": "completed", "messages": [{"item_id": "late-item", "text": "generic OWN late output"}], "error": None}}
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(original, {"type": "inventory", "certainty": "known",
                      "workers": [observation]}), False)
        self.assertEqual(self.host.view(key), before)
        self.assertEqual(self.host.state.load(self.host.state_dir, *key), task)


def selection_case(name, changes):
    def test(self):
        key, identity = self.pending()
        saved = self.host.tasks[key]
        healthy = self.host.binding(key)
        before = copy.deepcopy(self.host.view(key))
        translated = {field: getattr(self.host.state.Stage, value) if field == "stage" else value
                      for field, value in changes.items()}
        self.host.save(key, **translated)
        notifier = self.host.present(key)
        self.assert_no_effect(identity, notifier)
        self.assertEqual(self.host.view(key), before)
        self.assertEqual(self.host.binding(key), healthy)
        self.host.state.save(self.host.state_dir, saved)
        self.host.tasks[key] = saved
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)
    test.__name__ = f"test_H20_current_task_{name}_suppresses_claim_and_effect"
    return test


for _name, _changes in {
    "park": {"park": "known-park"}, "stage": {"stage": "PLAN"},
    "ticket": {"ticket_cursor": 2}, "worktree": {"worktree": "/owned-mismatching-worktree"},
}.items():
    setattr(HostAcceptance, f"test_H20_current_task_{_name}_suppresses_claim_and_effect", selection_case(_name, _changes))


def signal_case(mode):
    def test(self):
        key, identity = self.pending()
        signal = Path(self.host.tasks[key].worktree) / ".agent" / "stage.json"
        if mode == "missing":
            signal.unlink()
        elif mode == "unreadable":
            signal.unlink()
            signal.mkdir()
        else:
            self.host.signal(key, "blocked", note="OWN nonworking stage gate")
        notifier = self.host.present(key)
        self.assert_pending_effect(key, identity, notifier)
        completion = self.host.completion(key, "gated-extra")
        self.host.apply(key, {"type": "turn/started", "thread_id": self.host.root_id(key),
                              "turn_id": "OWN-signal-gate-active"})
        before = copy.deepcopy(self.host.view(key))
        event = {"type": "delivery/proposed", "observed_revision": before["revision"],
                 "batch_id": "must-not-propose", "completion_ids": [completion["identity"]],
                 "input": [{"type": "text", "text": json.dumps([completion])}]}
        self.assertIs(self.host.client.event(self.host.binding(key), event), False)
        self.assertEqual(self.host.view(key), before)
        # Existing D was proposed before the bad signal; a second safe attempt cannot
        # bypass that signal either, after D's known rejection clears its own hold.
        self.host.reject(key)
        revision = self.host.view(key)["revision"]
        blocked = {"type": "delivery/sent", "observed_revision": revision, "batch_id": "D",
                   "attempt_id": "blocked-second-attempt", "client_message_id": "blocked-second-client",
                   "method": "turn/steer", "thread_id": self.host.root_id(key),
                   "expected_turn_id": self.host.view(key)["main"]["turn_id"]}
        before = copy.deepcopy(self.host.view(key))
        self.assertIs(self.host.client.event(self.host.binding(key), blocked), False)
        self.assertEqual(self.host.view(key), before)
        if signal.is_dir():
            signal.rmdir()
        self.host.signal(key, "working")
        event["observed_revision"] = self.host.view(key)["revision"]
        self.assertIs(self.host.client.event(self.host.binding(key), event), True,
                      "healthy signal must independently admit this same unassigned outcome")
        blocked["observed_revision"] = self.host.view(key)["revision"]
        self.assertIs(self.host.client.event(self.host.binding(key), blocked), True,
                      "healthy signal must independently admit this same exact active-steer attempt")
    test.__name__ = f"test_H32_{mode}_signal_allows_presentation_but_not_result_admission"
    return test


for _mode in ["missing", "unreadable", "nonworking"]:
    setattr(HostAcceptance, f"test_H32_{_mode}_signal_allows_presentation_but_not_result_admission", signal_case(_mode))


def stage_case(stage, eligible):
    def test(self):
        key, identity = self.compatibility(stage=stage, unbound=True)
        notifier = self.host.present(key)
        if eligible:
            self.assert_pending_effect(key, identity, notifier)
            expected = "spec" if stage == "AWAITING_SPEC_REVIEW" else self.host.tasks[key].stage.value
            self.assertEqual(self.host.binding(key)["stage"], expected)
            self.assertEqual(self.host.binding(key)["ticket"], "1" if stage == "IMPLEMENT" else "")
        else:
            self.assert_no_effect(identity, notifier)
            self.assertNotIn("presentation", self.host.alert(key, identity))
    test.__name__ = f"test_H33_saved_stage_{stage.lower()}_{'eligible' if eligible else 'ineligible'}"
    return test


for _stage in ["QUEUED", "SPEC", "AWAITING_SPEC_REVIEW", "PLAN", "IMPLEMENT", "REVIEW", "ADDRESS_REVIEW", "BLOCKED", "STALLED_ON_BUDGET"]:
    _name = f"test_H33_saved_stage_{_stage.lower()}_eligible"
    setattr(HostAcceptance, _name, stage_case(_stage, True))
for _stage in ["PR_OPEN", "FAILED", "DONE", "CANCELED"]:
    _name = f"test_H33_saved_stage_{_stage.lower()}_ineligible"
    setattr(HostAcceptance, _name, stage_case(_stage, False))


def damaged_evidence_case(kind, mode):
    def test(self):
        key, identity = self.pending()
        if kind == "task":
            paths = self.host.generated_task_paths[key]
            self.assertEqual(len(paths), 1, "OWN save artifact must be isolated before negative filesystem fault")
            path = paths[0]
        else:
            path = self.host.snapshot_path(key)
        original = path.read_bytes()
        if mode == "missing":
            path.unlink()
        elif mode == "unreadable":
            path.unlink()
            path.mkdir()
        else:
            path.write_bytes(b"invalid fixture artifact; not runtime evidence")
        notifier = self.host.present(key)
        self.assert_no_effect(identity, notifier)
        # Restore only after stopping/awaiting listener: negative filesystem faults
        # never manufacture a positive claim/receipt or race the sole runtime writer.
        self.host.listener.close()
        if path.is_dir():
            path.rmdir()
        path.write_bytes(original)
        self.host.listener.start()
        self.host.present(key, notifier)
        self.assert_pending_effect(key, identity, notifier)
    test.__name__ = f"test_H34_{mode}_{kind}_evidence_grants_no_presentation"
    return test


for _kind in ["task", "runtime"]:
    for _mode in ["missing", "unreadable", "invalid"]:
        _name = f"test_H34_{_mode}_{_kind}_evidence_grants_no_presentation"
        setattr(HostAcceptance, _name, damaged_evidence_case(_kind, _mode))


def effect_change_case(kind):
    def test(self):
        key, identity = self.pending()
        original_task = self.host.tasks[key]
        binding = copy.deepcopy(self.host.binding(key))
        snapshot_path = self.host.snapshot_path(key)
        changes = {"stage": {"stage": self.host.state.Stage.PLAN}, "ticket": {"ticket_cursor": 2},
                   "worktree": {"worktree": str(self.host.root / "different-owned-worktree")}}
        def action(record):
            self.assertIs(record["value"], True, "eligibility fault follows a real admitted claim")
            if kind in changes:
                changed = self.host.save(key, **changes[kind])
                self.assertEqual(self.host.state.load(self.host.state_dir, *key), changed)
            elif kind == "replacement":
                self.host.task(*key, ticket=2)
                self.assertNotEqual(self.host.binding(key)["launch_id"], binding["launch_id"])
            else:
                self.assertEqual(self.host.client.retire(binding, self.host.view(key)["revision"], reason="forced"), "retired")
        self.host.listener.proxy.after_new_claim = action
        notifier = self.host.present(key)
        self.assertTrue(self.host.listener.proxy.claim_committed.is_set())
        retained = json.loads(snapshot_path.read_text())
        alert, = [item for item in retained["alerts"] if item.get("batch_id") == identity["batch_id"]]
        self.assertGreater(alert["presentation"]["pending_claim_revision"], 0)
        self.assert_no_effect(identity, notifier)
        if kind in changes:
            self.host.state.save(self.host.state_dir, original_task)
            self.host.tasks[key] = original_task
            self.host.present(key, notifier)
            self.assert_no_effect(identity, notifier)
    test.__name__ = f"test_H22_known_{kind}_after_claim_suppresses_both_effects"
    return test


for _kind in ["stage", "ticket", "worktree", "replacement", "retirement"]:
    _name = f"test_H22_known_{_kind}_after_claim_suppresses_both_effects"
    setattr(HostAcceptance, _name, effect_change_case(_kind))


if __name__ == "__main__":
    unittest.main()
