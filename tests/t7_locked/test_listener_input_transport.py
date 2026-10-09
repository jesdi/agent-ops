"""Both existing Unix HTTP clients carry optional provenance on the existing route."""
import inspect
import unittest
from dispatcher.runtime_http import BoundClient
from .layered_dispatcher_fixture import LayeredDispatcherFixture
from .receipt_events import history

class ListenerInputTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=LayeredDispatcherFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start()
    async def exercise(self,client):
        self.assertIn('native_input',inspect.signature(client.accept_input).parameters,'T7_HTTP_NATIVE_INPUT_CAPABILITY_UNAVAILABLE');content=[{'type':'text','text':' Exact HTTP λ\n'}];native={'method':'turn/steer','thread_id':self.flow.root,'expected_turn_id':self.flow.turn,'input':content};self.assertTrue(client.accept_input(self.flow.binding,'http-ordinary',native_input=native));before=self.flow.view();self.assertEqual(before['inputs']['http-ordinary']['native_input'],native);self.assertEqual(before['inputs']['http-ordinary']['revision'],before['revision']);self.assertFalse(client.accept_input(self.flow.binding,'http-ordinary',native_input=native));self.assertEqual(self.flow.view(),before)
        self.assertTrue(client.event(self.flow.binding,history(self.flow.root,'http-ordinary',self.flow.turn,'http-item',[{'type':'text','text':' Exact HTTP λ\n','text_elements':[]}])));after=self.flow.view();self.assertEqual(after['inputs']['http-ordinary']['status'],'accepted');self.assertEqual(after['inputs']['http-ordinary']['native_input'],native);self.assertEqual(after['inputs']['http-ordinary']['revision'],before['inputs']['http-ordinary']['revision']);self.assertEqual(after['main'],before['main'])
    async def test_host_runtimeclient_optional_provenance_uses_real_existing_listener_input_route(self):await self.exercise(self.flow.host)
    async def test_boundclient_optional_provenance_uses_real_existing_listener_input_route(self):await self.exercise(BoundClient(self.flow.state,self.flow.target,self.flow.issue,self.flow.binding['launch_id']))
