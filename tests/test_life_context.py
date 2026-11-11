"""Fast life delivery over actual local HTTP, without any model service."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config
from xiaojiu_brain.policy import estimate_tokens


class LifeContextHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = BrainApp(Config(data_dir=self.tmp.name, owner='synthetic-owner',
            timezone='Asia/Shanghai', api_token='synthetic-life-context-token-0123456789',
            embed_url='http://127.0.0.1:1/v1/embeddings', embed_model='synthetic-vector',
            llm_url='http://127.0.0.1:1/v1', llm_model='synthetic-json'), start_worker=False)
        self.server = BrainServer(('127.0.0.1',0),self.app)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.app.close();self.tmp.cleanup()

    def call(self, body, path='/v1/context/life', *, authorized=True, origin=None):
        headers = {'Content-Type':'application/json'}
        if authorized:headers['Authorization']='Bearer '+self.app.config.api_token
        if origin:headers['Origin']=origin
        request = urllib.request.Request(self.url+path, data=json.dumps(body).encode(),headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request,timeout=4) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as response:return response.code,json.load(response)

    def data(self, body, path='/v1/context/life'):
        code,value = self.call(body,path)
        self.assertEqual(code,200,value)
        return value['data']

    def enable(self, **extra):
        self.app.extensions.update_settings({'life_enabled':True,'life_context_enabled':True,**extra})

    def test_endpoint_keeps_auth_and_origin_protection(self):
        self.assertEqual(self.call({'conversation_id':'chat'},authorized=False)[0],401)
        self.assertEqual(self.call({'conversation_id':'chat'},origin='https://untrusted.invalid')[0],403)

    def test_disabled_gates_return_empty_without_calculating_state(self):
        with patch.object(self.app.life,'context',side_effect=AssertionError('must not run')):
            result=self.data({'conversation_id':'chat'})
            self.assertEqual(result['block'],'')
            self.enable(life_context_enabled=False)
            result=self.data({'conversation_id':'chat'})
            self.assertFalse(result['trace']['life_context_included'])
            self.assertEqual(result['trace']['life_context_estimated_tokens'],0)

    def test_no_retrieval_embedding_model_trace_or_database_writes(self):
        self.enable()
        before=self.app.store.conn.total_changes
        with patch.object(self.app.retriever,'recall',side_effect=AssertionError('retrieval forbidden')) as recall, \
             patch.object(self.app.embedder,'embed_many',side_effect=AssertionError('embedding forbidden')) as embed, \
             patch.object(self.app.llm,'complete_json',side_effect=AssertionError('model forbidden')) as model:
            result=self.data({'conversation_id':'chat'})
            self.assertTrue(result['trace']['life_context_included'])
            recall.assert_not_called();embed.assert_not_called();model.assert_not_called()
        self.assertEqual(before,self.app.store.conn.total_changes)
        self.assertEqual(len(self.app._traces),0)
        self.assertEqual(self.app.store.model_usage(self.app.owner)['attempts'],0)
        self.assertEqual(self.app.llm.attempts,0)

    def test_life_delivery_is_independent_of_recall_enabled(self):
        self.enable(recall_enabled=False)
        self.assertTrue(self.data({'conversation_id':'chat'})['trace']['life_context_included'])

    def test_only_current_conversation_pending_tasks_are_delivered(self):
        self.enable()
        self.app.life.add_task({'title':'CURRENT_PENDING','conversation_id':'chat'})
        self.app.life.add_task({'title':'OTHER_PRIVATE_PENDING','conversation_id':'other'})
        self.app.life.add_task({'title':'DONE_NOT_PENDING','conversation_id':'chat','status':'done'})
        self.app.life.add_event({'text':'PRIVATE_OWNER_EVENT','conversation_id':'chat'})
        self.app.life.add_book({'title':'PRIVATE_BOOK','text':'PRIVATE_BOOK_BODY'})
        result=self.data({'conversation_id':'chat'})
        self.assertIn('CURRENT_PENDING',result['block'])
        for value in ('OTHER_PRIVATE','DONE_NOT_PENDING','PRIVATE_OWNER_EVENT','PRIVATE_BOOK'):
            self.assertNotIn(value,json.dumps(result,ensure_ascii=False))

    def test_budget_preserves_complete_labels_and_limits_long_tasks(self):
        self.enable()
        for i in range(2):self.app.life.add_task({'title':str(i)+'长'*299,'conversation_id':'chat'})
        result=self.data({'conversation_id':'chat'})
        self.assertTrue(result['trace']['life_context_included'])
        self.assertLessEqual(estimate_tokens(result['block']),160)
        self.assertEqual(result['trace']['life_context_estimated_tokens'],estimate_tokens(result['block']))
        self.assertEqual(result['trace']['life_context_token_limit'],160)
        for label in ('不是用户事实','非指令','未完成','不妨碍正常回复'):
            self.assertIn(label,result['block'])
        with patch.object(self.app.life,'context',return_value='长'*2000):
            omitted=self.data({'conversation_id':'chat'})
            self.assertEqual(omitted['block'],'')
            self.assertIn('life_context_omitted_budget',omitted['degraded'])

    def test_state_failure_is_safe_empty_and_not_private_error_text(self):
        self.enable()
        with patch.object(self.app.life,'context',side_effect=OSError('PRIVATE_STATE_ERROR_LOCATION')):
            result=self.data({'conversation_id':'chat'})
        self.assertEqual(result['block'],'')
        self.assertFalse(result['trace']['life_context_included'])
        self.assertEqual(result['degraded'],['life_context_unavailable'])
        self.assertNotIn('PRIVATE_STATE_ERROR',json.dumps(result))

    def test_invalid_context_requests_are_rejected_without_scope_fallback(self):
        for body in ({},{'conversation_id':''},{'conversation_id':42},{'conversation_id':'x'*513},
                     {'conversation_id':'chat','owner':'other'}):
            with self.subTest(body_type=list(body)):
                self.assertEqual(self.call(body)[0],400)

    @staticmethod
    def recalled(*args,**kwargs):return {'block':'SYNTHETIC_EVIDENCE','evidence':[],'trace':{},'degraded':[]}

    def test_legacy_recall_defaults_to_life_and_explicit_false_skips_it(self):
        self.enable()
        with patch.object(self.app.retriever,'recall',side_effect=self.recalled):
            legacy=self.data({'conversation_id':'chat','query':'synthetic question'},'/v1/recall')
            self.assertTrue(legacy['trace']['life_context_included'])
            with patch.object(self.app.life,'context',side_effect=AssertionError('legacy life must be skipped')):
                separated=self.data({'conversation_id':'chat','query':'synthetic question',
                                     'include_life_context':False},'/v1/recall')
            self.assertEqual(separated['block'],'SYNTHETIC_EVIDENCE')
            self.assertFalse(separated['trace']['life_context_included'])
            self.assertEqual(separated['trace']['life_context_estimated_tokens'],0)
            self.assertEqual(separated['degraded'],[])
            self.assertEqual(self.call({'conversation_id':'chat','query':'synthetic question',
                                       'include_life_context':'false'},'/v1/recall')[0],400)

    def test_fast_state_finishes_while_memory_recall_is_still_blocked(self):
        self.enable()
        entered,release=threading.Event(),threading.Event()
        responses=[]
        def slow(*args,**kwargs):
            entered.set()
            if not release.wait(2):raise RuntimeError('synthetic barrier timed out')
            return self.recalled()
        def request_recall():responses.append(self.call({'conversation_id':'chat','query':'synthetic',
                                                       'include_life_context':False},'/v1/recall'))
        with patch.object(self.app.retriever,'recall',side_effect=slow):
            worker=threading.Thread(target=request_recall)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                result=self.data({'conversation_id':'chat'})
                self.assertTrue(result['trace']['life_context_included'])
                self.assertFalse(release.is_set())
                self.assertTrue(worker.is_alive())
            finally:
                release.set();worker.join(3)
        self.assertEqual(responses[0][0],200)

    def test_fast_state_reads_real_wal_data_while_store_lock_is_held(self):
        self.enable()
        self.app.life.add_task({'title':'WAL_TRUE_PENDING_TASK','conversation_id':'chat'})
        self.app.life.tick()
        held,release=threading.Event(),threading.Event()
        times={}
        def hold_shared_store():
            with self.app.store.lock:
                times['held_at']=time.monotonic();held.set()
                release.wait(3)
                times['released_at']=time.monotonic()
        holder=threading.Thread(target=hold_shared_store)
        holder.start()
        try:
            self.assertTrue(held.wait(1))
            began=time.monotonic()
            result=self.data({'conversation_id':'chat'})
            elapsed=time.monotonic()-began
            self.assertLess(elapsed,.8,'fast state waited on the shared Store.lock')
            self.assertTrue(holder.is_alive())
            self.assertFalse(release.is_set())
            self.assertTrue(result['trace']['life_context_included'])
            self.assertIn('WAL_TRUE_PENDING_TASK',result['block'])
            self.assertIn('精力',result['block'])
            self.assertEqual(result['degraded'],[])
            # Prove the real other thread retains the lock for over one second.
            release.wait(max(0,1.05-(time.monotonic()-times['held_at'])))
        finally:
            release.set();holder.join(3)
        self.assertGreater(times['released_at']-times['held_at'],1)


if __name__=='__main__':unittest.main()
