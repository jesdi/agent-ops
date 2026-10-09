"""Genuine supervisor CLI keeps physical service/TUI while control reconnects."""
import copy
import unittest
from .external_fixture import ExternalFixture,eventually

class GenuineCLIReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=ExternalFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start()
    async def test_actual_cli_control_reconnect_retains_initial_receipt_same_session_and_live_remote_terminal(self):
        before=copy.deepcopy(self.flow.view());self.assertIn('bootstrap',before,'T7_GENUINE_CLI_BOOTSTRAP_PROVENANCE_UNAVAILABLE');self.assertEqual(before['bootstrap']['root_status'],'bound');initial=before['bootstrap']['initial_input']['client_message_id'];self.assertEqual(before['inputs'][initial]['status'],'accepted');pid=self.flow.supervisor.pid
        names={r['connection_id']:r['payload']['params']['clientInfo']['name'] for r in self.flow.records() if r['kind']=='client-request' and r['payload']['method']=='initialize'};current=set((await self.flow.control('connections'))['connection_ids']);owned=current-{key for key,name in names.items() if name in ['own-terminal','own-observation']};self.assertTrue(owned,'OWN_CLI_CONTROL_CONNECTION_NOT_IDENTIFIED');baseline_connections=current;controller_names={names[key] for key in owned if key in names}-{'own-terminal','own-observation'}
        for connection in owned:await self.flow.control('disconnect',connection_id=connection)
        response=await self.flow.terminal('input',text='Generic actual CLI reconnect operator',client_message_id='cli-reconnect-operator');self.assertIn('result',response);self.assertIsNone(self.flow.supervisor.poll());self.assertEqual(self.flow.supervisor.pid,pid)
        async def resubscribed():
            records=self.flow.records();names={r['connection_id']:r['payload']['params']['clientInfo']['name'] for r in records if r['kind']=='client-request' and r['payload']['method']=='initialize'}
            requests=[r for r in records if r['kind']=='client-request' and r['connection_id'] not in baseline_connections and names.get(r['connection_id']) in controller_names and r['payload']['method']=='thread/resume' and r['payload']['params']['threadId']==self.flow.root]
            return any(reply['kind']=='server-response' and reply['method']=='thread/resume' and reply['connection_id']==request['connection_id'] and type(reply['payload']['id']) is type(request['payload']['id']) and reply['payload']['id']==request['payload']['id'] and 'error' not in reply['payload'] and reply['payload'].get('result',{}).get('thread',{}).get('id')==self.flow.root for request in requests for reply in records)
        await eventually(resubscribed,'T7_GENUINE_CLI_CONTROL_NOT_EXACT_ROOT_RESUBSCRIBED')
        command=await self.flow.command('generic actual CLI reconnect sentinel');await self.flow.qualify([command]);identity=await self.flow.finish(command)
        await eventually(lambda:next((b for b in self.flow.view()['deliveries'] if identity in b['completion_ids'] and b['status']=='confirmed'),None),'T7_GENUINE_CLI_RECONNECT_NO_DURABLE_SENTINEL_ACCEPTANCE');after=self.flow.view();self.assertEqual(after['binding'],before['binding']);self.assertEqual(after['bootstrap'],before['bootstrap']);self.assertEqual(self.flow.sessions.runtime_view(self.flow.target,self.flow.issue)['binding'],before['binding']);self.assertEqual(sum(r['kind']=='native-accepted' and r['item'].get('clientId')==initial for r in self.flow.records()),1)
        self.assertTrue(any(r['kind']=='client-request' and r['connection_id'] not in owned and r['payload']['method']=='thread/resume' and r['payload']['params']['threadId']==self.flow.root for r in self.flow.records()));self.assertEqual(after['history_checkpoint']['baseline_turns'],before['history_checkpoint']['baseline_turns'])
