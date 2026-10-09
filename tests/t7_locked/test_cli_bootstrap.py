"""Genuine supervisor CLI observes bootstrap reservation before native mutation."""
import asyncio
import json
import unittest
from websockets.asyncio.client import unix_connect
from .external_fixture import ExternalFixture,eventually

async def cancel_start(task):
    task.cancel();await asyncio.gather(task,return_exceptions=True)

class SupervisorBootstrapCLITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.flow=ExternalFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close)
    async def barrier_start(self,command):
        self.flow.env['T7_PROVIDER_STARTUP_CONTROLS']=json.dumps([command]);task=asyncio.create_task(self.flow.start());self.addAsyncCleanup(cancel_start,task)
        await eventually(lambda:(self.flow.run/'ready.json').exists() and (self.flow.run/'startup-controls.json').exists(),'SETUP_GENUINE_CLI_PROVIDER_NOT_READY')
        socket=await unix_connect(str(self.flow.run/'control.sock'));self.addAsyncCleanup(socket.close)
        async def control(op,**fields):
            await socket.send(json.dumps({'op':op,**fields}));response=json.loads(await socket.recv());self.assertNotIn('fixture_error',response);return response['result']
        barrier=json.loads((self.flow.run/'startup-controls.json').read_text())[0]['result']['barrier_id']
        async def hit():return (await control('root-reply-barrier-status' if command['op']=='root-reply-barrier-arm' else 'barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'SETUP_GENUINE_CLI_NATIVE_BARRIER_NOT_HIT');self.assertIsNone(self.flow.supervisor.poll());return task,barrier,control
    async def test_genuine_cli_root_operation_is_durable_before_initial_native_root_acceptance(self):
        task,barrier,control=await self.barrier_start({'op':'root-reply-barrier-arm','method':'thread/start','point':'before-native-operation'})
        view=self.flow.view();self.assertIn('bootstrap',view,'T7_GENUINE_CLI_ROOT_FORWARDED_WITHOUT_DURABLE_ATTEMPT');record=view['bootstrap'];self.assertIsNotNone(record)
        self.assertEqual(record['root_status'],'attempted-unconfirmed');self.assertEqual(record['root_method'],'thread/start');self.assertIsNone(record['requested_conversation_id']);self.assertIsNone(view['binding']['conversation_id']);self.assertGreater(record['root_attempt_revision'],0)
        self.assertEqual(record['initial_input']['input'],[{'type':'text','text':'Generic initial stage work.'}]);self.assertNotIn(record['initial_input']['client_message_id'],view['inputs'])
        await control('root-reply-barrier-release',barrier_id=barrier);await task;self.assertEqual(self.flow.view()['bootstrap']['root_status'],'bound');self.assertEqual(json.loads((self.flow.run/'terminal-ready.json').read_text())['root'],self.flow.root)
    async def test_genuine_cli_initial_pending_input_exists_before_native_validation_and_prompt_is_once(self):
        task,barrier,control=await self.barrier_start({'op':'barrier-arm','point':'before-input-validation','method':'turn/start','one_shot':True})
        packet=next(r['payload'] for r in self.flow.records() if r['kind']=='client-request' and r['payload']['method']=='turn/start');client=packet['params']['clientUserMessageId'];view=self.flow.view()
        self.assertEqual(view['inputs'][client]['status'],'pending');self.assertIsNone(view['inputs'][client]['turn_id']);self.assertIn('bootstrap',view,'T7_GENUINE_CLI_INITIAL_EXACT_INPUT_NOT_DURABLE')
        self.assertEqual(view['bootstrap']['initial_input'],{'client_message_id':client,'input':packet['params']['input']});self.assertEqual(view['bootstrap']['root_status'],'bound');self.assertFalse(any(r['kind']=='native-accepted' for r in self.flow.records()))
        await control('barrier-release',barrier_id=barrier);await task;self.assertEqual(sum(r['kind']=='native-accepted' and r['item'].get('clientId')==client for r in self.flow.records()),1);self.assertEqual(self.flow.view()['inputs'][client]['status'],'accepted')
