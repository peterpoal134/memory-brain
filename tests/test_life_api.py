"""Owner-authenticated life integration; synthetic data and zero model calls."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config


class LifeAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=BrainApp(Config(data_dir=self.tmp.name,owner='synthetic-owner',
            timezone='Asia/Shanghai',api_token='synthetic-life-api-token-0123456789'),start_worker=False)
        self.server=BrainServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.app.close();self.tmp.cleanup()

    def call(self,path,body=None,method=None,authorized=True):
        request=urllib.request.Request(self.url+path,
            data=None if body is None else json.dumps(body).encode(),method=method,
            headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+self.app.config.api_token} if authorized else {})})
        try:
            with self.opener.open(request,timeout=4) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as r:return r.code,json.load(r)

    def data(self,path,body=None,method=None):
        code,payload=self.call(path,body,method);self.assertEqual(code,200,payload)
        return payload['data']

    def test_all_life_routes_require_management_auth(self):
        for path,body,method in [('/v1/life',None,'GET'),('/v1/life/events',{},'POST'),
            ('/v1/life/tasks',None,'GET'),('/v1/life/books',None,'GET'),
            ('/v1/life/journal',None,'GET'),('/v1/life/tick',{},'POST')]:
            with self.subTest(path=path):self.assertEqual(self.call(path,body,method,False)[0],401)

    def test_disabled_then_enabled_state_and_no_implicit_memory_jobs(self):
        initial=self.data('/v1/life');self.assertFalse(initial['enabled'])
        self.assertFalse(self.data('/v1/life/tick',{})['ran'])
        before=self.app.store.stats(self.app.owner);jobs=self.app.store.job_stats()
        self.data('/v1/settings',{'life_enabled':True},'PATCH')
        self.assertTrue(self.data('/v1/life/tick',{})['ran'])
        self.assertFalse(self.data('/v1/life/tick',{})['ran'])
        self.assertEqual(before,self.app.store.stats(self.app.owner))
        self.assertEqual(jobs,self.app.store.job_stats())
        self.assertEqual(self.data('/v1/status')['model_usage']['attempts'],0)

    def test_life_crud_journal_and_export(self):
        event=self.data('/v1/life/events',{'text':'合成散步记录','conversation_id':'test-A'})
        self.assertEqual(event['type'],'owner_note')
        task=self.data('/v1/life/tasks',{'title':'合成待办','conversation_id':'test-A'})
        self.assertEqual(self.data('/v1/life/tasks/'+str(task['id']),{'status':'done'},'PATCH')['status'],'done')
        book=self.data('/v1/life/books',{'title':'合成文本','text':'这是供本地阅读的合成文本。'})
        self.assertEqual(len(self.data('/v1/life/books')['items']),1)
        journal=self.data('/v1/life/journal');self.assertIn('合成散步记录',journal['text'])
        self.assertEqual(journal['kind'],'deterministic_journal')
        code,exported=self.call('/v1/export');self.assertEqual(code,200);self.assertIn('life',exported)
        for kind,item in (('events',event),('tasks',task),('books',book)):
            self.assertTrue(self.data('/v1/life/'+kind+'/'+str(item['id']),{},'DELETE')['deleted'])
        self.assertEqual(self.data('/v1/life/events')['items'],[])

    def test_recall_life_side_channel_does_not_change_evidence_or_cross_conversation(self):
        self.data('/v1/ingest',{'conversation_id':'test-A','message_id':'original-1','role':'user','text':'合成雨伞放在合成柜里。'})
        query={'conversation_id':'test-A','query':'合成雨伞在哪里','token_budget':1200}
        before=self.data('/v1/recall',query)
        self.data('/v1/life/tasks',{'title':'A会话整理便签','conversation_id':'test-A'})
        self.data('/v1/life/tasks',{'title':'B秘密绝不串到A','conversation_id':'test-B'})
        self.data('/v1/settings',{'life_enabled':True},'PATCH')
        after=self.data('/v1/recall',query)
        self.assertEqual(before['evidence'],after['evidence'])
        self.assertTrue(after['trace']['life_context_included'])
        self.assertIn('A会话整理便签',after['block']);self.assertNotIn('B秘密',after['block'])
        self.assertLessEqual(after['trace']['estimated_tokens'],1360)
        self.assertLessEqual(after['trace']['life_context_estimated_tokens'],160)
        self.data('/v1/settings',{'life_context_enabled':False},'PATCH')
        disabled=self.data('/v1/recall',query)
        self.assertEqual(disabled['block'],before['block'])

    def test_full_evidence_budget_still_includes_bounded_life_without_evidence_eviction(self):
        self.data('/v1/settings',{'life_enabled':True},'PATCH')
        self.data('/v1/life/tasks',{'title':'检查合成待办','conversation_id':'test-A'})
        block='x'*3600  # Exactly 1200 under the existing UTF-8 estimator.
        frozen={'block':block,'evidence':[{'document_id':'frozen-original'}],
            'assertions':[],'conflicts':[],'coverage_gaps':[],'degraded':[],'trace':{}}
        with patch.object(self.app.retriever,'recall',return_value=frozen):
            result=self.data('/v1/recall',{'conversation_id':'test-A','query':'test','token_budget':1200})
        self.assertTrue(result['block'].startswith(block+'\n\n'))
        self.assertEqual(result['evidence'],[{'document_id':'frozen-original'}])
        self.assertTrue(result['trace']['life_context_included'])
        self.assertEqual(result['trace']['evidence_estimated_tokens'],1200)
        self.assertLessEqual(result['trace']['life_context_estimated_tokens'],160)
        self.assertLessEqual(result['trace']['estimated_tokens'],1360)

    def test_capture_uses_source_id_but_does_not_copy_message_body_into_life(self):
        self.data('/v1/settings',{'life_enabled':True},'PATCH')
        item=self.data('/v1/ingest',{'conversation_id':'test-A','message_id':'observed','role':'user','text':'合成私密正文只应存在原话中'})
        events=self.data('/v1/life/events')['items']
        self.assertEqual(len(events),1)
        self.assertNotIn('合成私密正文',json.dumps(events,ensure_ascii=False))
        self.data('/v1/messages/'+str(item['id']),{},'DELETE')
        self.assertNotIn('合成私密正文',json.dumps(self.data('/v1/life/events'),ensure_ascii=False))

    def test_invalid_settings_reject_atomically_and_record_unknown_input(self):
        before=self.data('/v1/settings')
        for patch in ({'life_interval_seconds':1},{'life_wake_time':'25:00'},
            {'life_wake_time':'08:00','life_sleep_time':'08:00'},
            {'life_enabled':True,'life_context_enabled':'true'}):
            self.assertEqual(self.call('/v1/settings',patch,'PATCH')[0],400)
            self.assertEqual(before,self.data('/v1/settings'))
        for path,body in (('/v1/life/events',{'text':'x','source':{'kind':'user'}}),
            ('/v1/life/tasks',{'title':'x','status':'invented'}),
            ('/v1/life/books',{'title':'x','text':'x'*100001}),
            ('/v1/life/books',{'title':'x','text':'a\x00b'})):
            self.assertEqual(self.call(path,body)[0],400)

    def test_online_backup_includes_life_storage(self):
        self.data('/v1/life/events',{'text':'备份合成记录'})
        result=self.data('/v1/backup',{})
        with zipfile.ZipFile(Path(self.tmp.name)/'backups'/result['file_name']) as z:
            self.assertIn('brain.db',z.namelist());self.assertIn('preferences.json',z.namelist())
            self.assertEqual(result['integrity']['integrity'],'ok')


if __name__=='__main__':unittest.main()
