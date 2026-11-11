"""Real local HTTP accounting and durable-ledger regression, without paid models."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.config import Config
from xiaojiu_brain.storage import Store


class UsageStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'brain.db';self.store=Store(self.path)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    @staticmethod
    def event(**patch):
        event={'attempt_id':'test-attempt','started_at':1700000000.,'elapsed_ms':12.,'model':'isolated-model',
          'outcome':'success','error':None,'finish_reason':'stop','usage':{'input_tokens':100,'output_tokens':20,
          'cache_hit_tokens':40,'cache_miss_tokens':60,'total_tokens':120},'request_context':{'batch_id':'test-batch','job_ids':[1,2]}}
        event.update(patch);return event
    def test_no_usage_does_not_invent_zero_tokens(self):
        stats=self.store.model_usage('owner')
        self.assertEqual(stats['attempts'],0);self.assertIsNone(stats['input_tokens']);self.assertIsNone(stats['output_tokens'])
    def test_duplicate_is_idempotent_and_other_owner_cannot_read_it(self):
        event=self.event();self.store.record_model_attempt('owner',event);self.store.record_model_attempt('owner',event)
        self.assertEqual(self.store.model_usage('owner')['attempts'],1)
        self.assertEqual(self.store.model_usage('other')['attempts'],0)
        with self.assertRaises(ValueError):self.store.record_model_attempt('other',event)
        with self.assertRaises(ValueError):self.store.record_model_attempt('owner',self.event(elapsed_ms=13))
    def test_safe_allowlist_does_not_persist_prompt_response_or_credentials(self):
        marker='synthetic-private-do-not-log';event=self.event()
        event.update(system=marker,user=marker,content=marker,api_key=marker,url=marker,error=marker)
        event['request_context'].update(prompt=marker,text=marker,batch_id=marker+'\ncontent')
        self.store.record_model_attempt('owner',event)
        serialized=self.store.conn.execute('SELECT event_json FROM model_attempts').fetchone()[0]
        self.assertNotIn(marker,serialized);self.assertEqual(self.store.model_usage('owner')['input_tokens'],100)
    def test_unknown_and_partial_usage_are_explicit_and_persist_across_restart(self):
        self.store.record_model_attempt('owner',self.event(usage=None,outcome='transport'))
        self.store.record_model_attempt('owner',self.event(attempt_id='partial',usage={'input_tokens':42}))
        self.store.close();self.store=Store(self.path);s=self.store.model_usage('owner')
        self.assertEqual((s['attempts'],s['reported_responses'],s['unknown_usage_attempts']),(2,1,1))
        self.assertEqual(s['input_tokens'],42);self.assertIsNone(s['output_tokens'])
        self.assertEqual(s['missing_fields']['output_tokens'],2)
    def test_additive_old_schema_migration_preserves_original_and_source(self):
        self.store.ingest('owner','chat','m1','user','原话仍在')
        self.store.conn.execute('DROP TABLE model_attempts')
        self.store.conn.execute('ALTER TABLE jobs DROP COLUMN batch_limit')
        self.store.close();self.store=Store(self.path)
        self.assertEqual(self.store.list_messages('owner')[0]['text'],'原话仍在')
        self.assertEqual(self.store.conn.execute('SELECT batch_limit FROM jobs LIMIT 1').fetchone()[0],12)
        self.assertEqual(self.store.model_usage('owner')['attempts'],0)


class LocalModelHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        packet=json.loads(body['messages'][1]['content']);messages=packet['messages']
        truncated=len(messages)>6
        docs=[] if truncated else [{'kind':'fact','text':m['text'],
            'source_refs':[{'message_id':m['message_id'],'quote':m['text']}]} for m in messages]
        reply={'choices':[{'finish_reason':'length' if truncated else 'stop',
            'message':{'content':json.dumps({'documents':docs},ensure_ascii=False)}}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':40,
                'prompt_cache_miss_tokens':60,'total_tokens':120}}
        data=json.dumps(reply).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)))
        self.end_headers();self.wfile.write(data)


class AppUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.model=ThreadingHTTPServer(('127.0.0.1',0),LocalModelHandler)
        self.model.requests=[];self.model_thread=threading.Thread(target=self.model.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.model_thread.start()
        self.cfg=Config(data_dir=self.tmp.name,owner='owner',api_token='synthetic-management-token-0123456789',
            llm_url=f'http://127.0.0.1:{self.model.server_port}/v1',llm_model='local-stub-model')
        self.app=BrainApp(self.cfg,start_worker=False)
    def tearDown(self):
        self.app.close();self.model.shutdown();self.model.server_close();self.model_thread.join(1);self.tmp.cleanup()
    def ingest(self,n):
        for i in range(n):self.app.ingest({'conversation_id':'chat','message_id':str(i),'role':'user','text':'合成来源'+str(i)})
    def test_split_attempts_and_status_meter_are_durable_through_real_http(self):
        self.ingest(12);result=self.app.worker.run_derivation_once()
        self.assertEqual((result['provider_calls'],result['splits'],result['derived']),(3,1,12))
        usage=self.app.status()['model_usage']
        self.assertEqual((usage['attempts'],usage['input_tokens'],usage['output_tokens']),(3,300,60))
        self.assertEqual(usage['by_outcome'],{'output_truncated':1,'success':2})
        for request in self.model.requests:self.assertEqual(request['max_tokens'],2400)
        rows=[json.loads(r[0]) for r in self.app.store.conn.execute('SELECT event_json FROM model_attempts')]
        self.assertEqual([len(r['request_context']['job_ids']) for r in rows],[12,6,6])
        self.assertEqual(len({r['request_context']['root_batch_id'] for r in rows}),1)
        self.assertNotIn('合成来源',json.dumps(rows,ensure_ascii=False))
        api=BrainServer(('127.0.0.1',0),self.app);thread=threading.Thread(target=api.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',api.server_port,timeout=3)
            conn.request('GET','/v1/status',headers={'Authorization':'Bearer '+self.cfg.api_token})
            response=conn.getresponse();body=response.read();conn.close()
            self.assertEqual(response.status,200);self.assertEqual(json.loads(body)['data']['model_usage']['attempts'],3)
            self.assertNotIn(self.cfg.api_token.encode(),body)
        finally:api.shutdown();api.server_close();thread.join(1)
        self.app.close();self.app=BrainApp(self.cfg,start_worker=False)
        self.assertEqual(self.app.status()['model_usage']['output_tokens'],60)
        self.assertEqual(self.app.store.stats('owner')['messages'],12)
    def test_broken_ledger_pauses_paid_work_and_does_not_invoke_split_children(self):
        self.ingest(12)
        def broken(event):raise OSError('synthetic disk failure')
        self.app.llm.attempt_recorder=broken
        result=self.app.worker.run_derivation_once()
        self.assertEqual(len(self.model.requests),1);self.assertTrue(self.app.worker.last_result['metering_paused'])
        self.assertEqual(self.app.store.job_stats()['failed'],12)
        self.app.ingest({'conversation_id':'chat','message_id':'new','role':'user','text':'另条原话'})
        self.app.worker.run_derivation_once(force=True)
        self.assertEqual(len(self.model.requests),1);self.assertEqual(self.app.store.stats('owner')['messages'],13)
        self.assertIn('usage_recording_error',self.app.worker.last_result['errors'])


if __name__=='__main__':unittest.main()
