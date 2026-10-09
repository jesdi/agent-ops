"""Unknown entry and ambiguous listener acknowledgments grant no send permission."""
import asyncio
import copy
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually

class AttachmentEntryGuardTests(unittest.IsolatedAsyncioTestCase):
    async def start(self,legacy=False):
        flow=CompositionFixture(name=self._testMethodName,legacy_bootstrap=legacy);self.addAsyncCleanup(flow.close);await flow.start();factory=flow.factory();self.assertTrue(callable(factory),'T7_ATTACHMENT_UNAVAILABLE_AFTER_HEALTHY_PUBLIC_SETUP');return flow,factory
    async def test_absent_retained_bootstrap_is_unknown_in_both_modes_and_never_retrofilled(self):
        flow,factory=await self.start(legacy=True);before=flow.view();self.assertNotIn('bootstrap',before)
        for mode in ['attach','first-launch']:
            entry=await flow.enter(factory,mode=mode,ready=False);waiting=asyncio.create_task(entry['handle'].wait_ready());foreign=(await flow.native.rpc('thread/start',{}))['result']['thread']['id'];self.assertIn('result',await flow.native.rpc('thread/read',{'threadId':foreign}));await flow.exit(entry);await asyncio.gather(waiting,return_exceptions=True)
            self.assertEqual(flow.view(),before)
        self.assertFalse(any(r['kind']=='native-accepted' for r in flow.records()));self.assertEqual(sum(r['kind']=='client-request' and r['payload']['method']=='thread/start' for r in flow.records()),2)
    async def test_unknown_boundless_default_attachment_never_consumes_explicit_null_first_launch_permission(self):
        flow,factory=await self.start();before=flow.view();self.assertIsNone(before['bootstrap']);entry=await flow.enter(factory,ready=False);waiting=asyncio.create_task(entry['handle'].wait_ready())
        self.assertIn('result',await flow.native.rpc('thread/list',{}));await flow.exit(entry);await asyncio.gather(waiting,return_exceptions=True);self.assertEqual(flow.view(),before);self.assertFalse(any(r['kind']=='client-request' and r['payload']['method'] in ['thread/start','turn/start'] for r in flow.records()))
    async def ambiguous_bootstrap(self,event_type):
        flow,factory=await self.start();proxy=await flow.use_proxy();rule=proxy.arm(event_type=event_type,route='/runtime/event');entry=await flow.enter(factory,mode='first-launch',ready=False)
        await eventually(lambda:proxy.hit(rule),'T7_REAL_BOOTSTRAP_ADMISSION_NOT_OBSERVED');await flow.exit(entry);before=copy.deepcopy(flow.view());record=before['bootstrap'];self.assertIsNotNone(record)
        if event_type=='bootstrap/root-attempted':
            self.assertEqual(record['root_status'],'attempted-unconfirmed');self.assertIsNone(before['binding']['conversation_id']);self.assertFalse(any(r['kind']=='client-request' and r['payload']['method'] in ['thread/start','turn/start'] for r in flow.records()))
        else:
            client=record['initial_input']['client_message_id'];self.assertEqual(before['inputs'][client]['status'],'pending');self.assertFalse(any(r['kind']=='native-accepted' for r in flow.records()))
        replacement=await flow.enter(factory,mode='first-launch',ready=False);self.assertIn('result',await flow.native.rpc('thread/list',{}));await flow.exit(replacement);self.assertEqual(flow.view()['bootstrap'],before['bootstrap']);self.assertFalse(any(r['kind']=='native-accepted' for r in flow.records()))
    async def test_accepted_root_attempt_with_lost_listener_response_is_held_before_native_forward(self):await self.ambiguous_bootstrap('bootstrap/root-attempted')
    async def test_accepted_initial_reservation_with_lost_listener_response_is_held_before_native_forward(self):await self.ambiguous_bootstrap('bootstrap/sent')
    async def invalid_public_view(self,mode):
        flow,factory=await self.start();proxy=await flow.use_proxy();before=flow.view();rule=proxy.arm(route='/runtime/view',mode=mode);closed=False
        try:
            entry=await flow.enter(factory,ready=False)
            try:await entry['handle'].wait_ready();self.fail('T7_INVALID_VIEW_INVENTED_READINESS')
            except (RuntimeError,ValueError,ConnectionError):closed=True
            finally:
                try:await flow.exit(entry)
                except (RuntimeError,ValueError,ConnectionError):closed=True
        except (RuntimeError,ValueError,ConnectionError):closed=True
        self.assertTrue(closed);self.assertTrue(proxy.hit(rule));self.assertEqual(flow.view(),before);self.assertFalse(any(r['kind']=='client-request' and r['payload']['method'] in ['thread/start','thread/resume','turn/start','turn/steer'] for r in flow.records()))
    async def test_missing_public_view_fails_entry_before_subscription_or_mutation(self):await self.invalid_public_view('null-view')
    async def test_malformed_public_view_fails_entry_before_subscription_or_mutation(self):await self.invalid_public_view('malformed-json')
