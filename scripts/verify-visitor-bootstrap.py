"""Offline contracts for anonymous visitor initialization and page recovery.

Only fixtures are used. No account, cookie value, or real platform is accessed.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from livewatch.providers import ProviderRegistry, ProbeError, _PROBE_CONTEXT


def page_response(identifier='123',live=True,resolved=None):
    payload={'roomStore':{'roomInfo':{'web_rid':identifier,'room':{
        'status':2 if live else 4,'id_str':'456','owner':{'nickname':'fixture'},
        'stream_url':{'hls_pull_url':'https://media.invalid/stream.m3u8'}}}}}
    body=('<script id="RENDER_DATA">'+json.dumps(payload)+'</script>').encode()
    return body,{'Content-Type':'text/html','_livewatch_http_status':200},resolved or 'https://live.douyin.com/'+identifier


def api_response(live=True):
    body=json.dumps({'data':{'data':[{'status':2 if live else 4,'id_str':'456','owner':{'nickname':'fixture'}}]}}).encode()
    return body,{'Content-Type':'application/json','_livewatch_http_status':200},'https://live.douyin.com/webcast/room/web/enter/'


EMPTY=(b'',{'_livewatch_http_status':200},'https://live.douyin.com/webcast/room/web/enter/')


class VisitorBootstrapChecks(unittest.TestCase):
    def setUp(self):
        self.provider=ProviderRegistry()
        self.room={'platform':'douyin','room_input':'123','input_type':'room','record_enabled':False,
                   'request_headers':{'Cookie':'sessionid=FIXTURE_SECRET','Authorization':'Bearer FIXTURE_SECRET'}}
        self.settings={'_status_only':True,'_probe_deadline':time.monotonic()+4,
                       'monitor_identity_isolation':True,'douyin_access_mode':'authorized',
                       'douyin_cookie':'sessionid=FIXTURE_SECRET'}

    def warm(self):
        self.provider._auth_state(self.settings)
        self.provider._local.status_bootstrapped=True

    def test_cold_page_confirms_target_then_reuses_visitor_transport_for_api(self):
        calls=[]
        def request(url,headers,*args,**kwargs):
            calls.append((url,dict(headers),kwargs['opener'],_PROBE_CONTEXT.deadline))
            return page_response() if len(calls)==1 else api_response(False)
        with patch('livewatch.providers._request',side_effect=request),patch.object(self.provider,'_enrich_douyin',side_effect=AssertionError('extra metadata request')):
            first=self.provider.check(self.room,self.settings)
            second=self.provider.check(self.room,self.settings)
        self.assertIs(first.live,True)
        self.assertTrue(first.stream_url)
        self.assertEqual(first.retry_after,0)
        self.assertIs(second.live,False)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][0],'https://live.douyin.com/123')
        self.assertIn('/webcast/room/web/enter/',calls[1][0])
        self.assertIs(calls[0][2],calls[1][2])
        self.assertAlmostEqual(calls[0][3],self.settings['_probe_deadline']-.25)
        self.assertEqual(calls[1][3],self.settings['_probe_deadline'])
        for _,headers,opener,_ in calls:
            self.assertNotIn('cookie',{key.lower() for key in headers})
            self.assertNotIn('FIXTURE_SECRET',str(headers))
            self.assertFalse(any(type(h).__name__=='_MonitorIdentityGuard' for h in opener.handlers))

    def test_cold_page_rejects_wrong_embedded_room_and_cross_room_redirect(self):
        for response in (page_response('999',resolved='https://live.douyin.com/123'),
                         page_response('123',resolved='https://live.douyin.com/999')):
            self.provider=ProviderRegistry()
            with self.subTest(response=response[2]),patch('livewatch.providers._request',return_value=response) as request:
                result=self.provider.check(self.room,self.settings)
            self.assertIsNone(result.live)
            self.assertEqual(result.error_code,'target_mismatch')
            self.assertEqual(request.call_count,1)

    def test_nested_page_and_selected_owner_identity_must_match_requested_room(self):
        for declared in ('roomInfo','room','owner'):
            for matching in (False,True):
                info={'room':{'id_str':'456','status':4,'owner':{'nickname':'fixture'}}}
                target=info if declared=='roomInfo' else info['room'] if declared=='room' else info['room']['owner']
                target['web_rid']='123' if matching else '999'
                payload={'props':{'pageProps':{'roomInfo':info}}}
                body=('<script id="__NEXT_DATA__">'+json.dumps(payload)+'</script>').encode()
                self.provider=ProviderRegistry()
                with self.subTest(declared=declared,matching=matching),patch('livewatch.providers._request',return_value=(body,{},'https://live.douyin.com/123')) as request:
                    result=self.provider.check(self.room,self.settings)
                self.assertEqual(request.call_count,1)
                if matching:
                    self.assertIs(result.live,False)
                    self.assertEqual(result.error_code,'')
                else:
                    self.assertIsNone(result.live)
                    self.assertEqual(result.error_code,'target_mismatch')

    def test_unrecognized_initial_page_can_use_compact_api_within_same_deadline(self):
        with patch('livewatch.providers._request',side_effect=[(b'<html></html>',{},'https://live.douyin.com/123'),api_response()]) as request:
            result=self.provider.check(self.room,self.settings)
        self.assertIs(result.live,True)
        self.assertEqual(request.call_count,2)
        self.assertEqual(result.retry_after,0)

    def test_empty_warm_api_recovers_via_page_and_obeys_page_cooldown(self):
        self.warm()
        with patch('livewatch.providers._request',side_effect=[EMPTY,page_response(),EMPTY]) as request:
            first=self.provider.check(self.room,self.settings)
            second=self.provider.check(self.room,self.settings)
        self.assertIs(first.live,True)
        self.assertEqual(first.retry_after,0)
        self.assertIn('stage=room_page',first.diagnostic)
        self.assertIsNone(second.live)
        self.assertEqual(request.call_count,3)

    def test_444_does_not_request_fallback_and_retry_after_is_respected(self):
        self.warm()
        with patch('livewatch.providers._request',side_effect=[ProbeError('limited','rate_limited',60,'http=444'),page_response(live=False)]) as request:
            result=self.provider.check(self.room,self.settings)
        self.assertIsNone(result.live)
        self.assertEqual(result.retry_after,60)
        self.assertEqual(request.call_count,1)
        self.provider=ProviderRegistry();self.warm()
        with patch('livewatch.providers._request',side_effect=ProbeError('limited','rate_limited',60,'http=429 retry_after=60')) as request:
            result=self.provider.check(self.room,self.settings)
        self.assertIsNone(result.live)
        self.assertEqual(result.retry_after,60)
        self.assertEqual(request.call_count,1)

    def test_short_remaining_budget_defers_page_then_recovers_without_repeating_api(self):
        self.warm()
        now=[100.0]
        def late_api(*args,**kwargs):
            now[0]=103.5
            return EMPTY
        with patch('livewatch.providers.time.monotonic',side_effect=lambda:now[0]):
            with patch('livewatch.providers._request',side_effect=late_api) as request:
                result=self.provider.check(self.room,self.settings|{'_probe_deadline':104})
            self.assertIsNone(result.live)
            self.assertEqual(request.call_count,1)
            with patch('livewatch.providers._request',return_value=page_response()) as request:
                result=self.provider.check(self.room,self.settings|{'_probe_deadline':107.5})
            self.assertIs(result.live,True)
            self.assertEqual(result.retry_after,0)
            self.assertEqual(request.call_count,1)
            self.assertEqual(request.call_args.args[0],'https://live.douyin.com/123')

    def test_cold_probe_with_no_return_budget_starts_no_network_operation(self):
        with patch('livewatch.providers._request') as request:
            result=self.provider.check(self.room,self.settings|{'_probe_deadline':time.monotonic()+.2})
        self.assertIsNone(result.live)
        self.assertEqual(result.error_code,'platform_unavailable')
        request.assert_not_called()

    def test_room_page_budget_from_parent_survives_worker_switch(self):
        self.warm()
        settings=self.settings|{'_status_page_not_before':time.monotonic()+30}
        with patch('livewatch.providers._request',return_value=EMPTY) as request:
            result=self.provider.check(self.room,settings)
        self.assertIsNone(result.live)
        self.assertEqual(request.call_count,1)
        self.assertIn('/webcast/room/web/enter/',request.call_args.args[0])
        # A newly created worker still needs its own anonymous visitor session.
        self.provider=ProviderRegistry()
        with patch('livewatch.providers._request',return_value=page_response()) as request:
            result=self.provider.check(self.room,settings)
        self.assertIs(result.live,True)
        self.assertEqual(result.retry_after,0)
        self.assertEqual(request.call_count,1)
        self.assertEqual(request.call_args.args[0],'https://live.douyin.com/123')

    def test_cold_network_failure_keeps_parent_deadline_and_reports_unknown(self):
        seen=[]
        def fail(*args,**kwargs):
            seen.append(_PROBE_CONTEXT.deadline)
            raise ProbeError('timeout','network_error',30,'network=deadline')
        with patch('livewatch.providers._request',side_effect=fail):
            result=self.provider.check(self.room,self.settings)
        self.assertIsNone(result.live)
        self.assertEqual(result.error_code,'network_error')
        self.assertIn('stage=room_page',result.diagnostic)
        self.assertAlmostEqual(seen[0],self.settings['_probe_deadline']-.25)
        self.assertIsNone(getattr(_PROBE_CONTEXT,'deadline',None))


if __name__=='__main__':
    unittest.main(verbosity=2)
