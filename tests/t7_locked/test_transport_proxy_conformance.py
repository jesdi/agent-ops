"""Own external Unix HTTP fault adapter conformance; no T7 acceptance claim."""
import json
from http.client import RemoteDisconnected
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import ARTIFACTS

class TransportProxyConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_proxy_preserves_real_exact_launch_http_and_drops_only_selected_response(self):
        flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(flow.close);await flow.start();proxy=await flow.use_proxy();before=flow.view();self.assertEqual(flow.bound.view(),before)
        rule=proxy.arm(event_type='service',route='/runtime/event')
        with self.assertRaises(RemoteDisconnected):flow.bound.event(flow.binding,{'type':'service','status':'unknown'})
        self.assertTrue(proxy.hit(rule));self.assertEqual(flow.view()['service'],'unknown')
        self.assertTrue(flow.host.event(flow.binding,{'type':'service','status':'live'}));self.assertEqual(flow.bound.view(),flow.view());self.assertEqual(flow.view()['binding'],before['binding']);self.assertIn('result',await flow.native.rpc('thread/list',{}))
        trace=[json.loads(line) for line in (ARTIFACTS/(flow.name+'-http-proxy.jsonl')).read_text().splitlines()];dropped=[r for r in trace if r.get('fault')=='drop-response'];self.assertEqual(len(dropped),1);self.assertEqual(dropped[0]['upstream_response'],'true')
        self.assertFalse(any(r['kind']=='native-accepted' for r in flow.records()))
        unchanged=flow.view();null_rule=proxy.arm(route='/runtime/view',mode='null-view');self.assertIsNone(flow.bound.view());self.assertTrue(proxy.hit(null_rule));self.assertEqual(flow.view(),unchanged)
        malformed_rule=proxy.arm(route='/runtime/view',mode='malformed-json')
        with self.assertRaises(json.JSONDecodeError):flow.bound.view()
        self.assertTrue(proxy.hit(malformed_rule));self.assertEqual(flow.view(),unchanged);self.assertEqual(flow.bound.view(),unchanged)
