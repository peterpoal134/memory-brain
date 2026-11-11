"""HTTP wiring for daily rules, explicit emotion events and bounded delivery."""
import http.client,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config

class StateRulesHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=BrainApp(Config(data_dir=self.tmp.name,owner='state-fixture',timezone='Asia/Shanghai',
            api_token='synthetic-state-rules-token-0123456789'),start_worker=False)
        self.server=BrainServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.app.close();self.tmp.cleanup()
    def call(self,path,body=None,method=None,auth=True):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={'Content-Type':'application/json'}
        if auth:headers['Authorization']='Bearer '+self.app.config.api_token
        c.request(method or ('GET' if body is None else 'POST'),path,None if body is None else json.dumps(body),headers)
        r=c.getresponse();status=r.status;data=json.loads(r.read());c.close();return status,data
    def data(self,path,body=None,method=None):
        status,result=self.call(path,body,method);self.assertEqual(status,200,result);return result['data']
    def test_rules_and_event_api_remain_authenticated_and_retry_safe(self):
        self.assertEqual(self.call('/v1/emotion/rules',auth=False)[0],401)
        self.assertEqual(self.call('/v1/life/rules',auth=False)[0],401)
        self.assertEqual(self.data('/v1/life/rules')['model'],'bounded_rhythm_v2')
        rules=self.data('/v1/emotion/rules');self.assertEqual(rules['cooldown_seconds'],300)
        args={'kind':'joy','intensity':.5,'idempotency_key':'synthetic-first'}
        first=self.data('/v1/emotion/events',args);self.assertTrue(first['applied'])
        replay=self.data('/v1/emotion/events',args);self.assertTrue(replay['replayed'])
        self.assertEqual(first['event']['id'],replay['event']['id'])
        cooldown=self.data('/v1/emotion/events',{**args,'idempotency_key':'synthetic-second'})
        self.assertFalse(cooldown['applied']);self.assertEqual(cooldown['reason'],'cooldown')
        self.assertEqual(self.data('/v1/emotion/events')['total'],1)
    def test_optional_pad_delivery_is_bounded_and_never_leaks_private_notes(self):
        self.app.extensions.update_settings({'life_enabled':True,'emotion_enabled':True})
        self.app.extensions.update_emotion({'pleasure':.4,'arousal':.2,'dominance':.1,
            'notes':'synthetic-private-note','tags':['synthetic-private-tag']})
        with patch.object(self.app.retriever,'recall',side_effect=AssertionError('unexpected retrieval')):
            result=self.data('/v1/context/life',{'conversation_id':'fixture'})
        self.assertTrue(result['trace']['life_context_included'])
        self.assertLessEqual(result['trace']['life_context_estimated_tokens'],160)
        self.assertIn('PAD',result['block'])
        self.assertNotIn('synthetic-private',result['block'])
    def test_rule_reading_does_not_write_or_count_assistant_as_a_person(self):
        self.app.extensions.update_settings({'life_enabled':True})
        self.data('/v1/ingest',{'conversation_id':'fixture','message_id':'assistant','role':'assistant',
            'text':'synthetic visible output','metadata':{'source_kind':'model'}})
        prefs=Path(self.tmp.name,'preferences.json').read_bytes();changes=self.app.store.conn.total_changes
        life=self.data('/v1/life');self.assertEqual(life['needs_model'],'bounded_rhythm_v2')
        self.assertEqual(life['needs_trace']['eligible_activity_counts']['chat'],0)
        for path in ('/v1/life/rules','/v1/emotion','/v1/emotion/rules','/v1/emotion/events'):self.data(path)
        self.assertEqual(self.app.store.conn.total_changes,changes)
        self.assertEqual(Path(self.tmp.name,'preferences.json').read_bytes(),prefs)
