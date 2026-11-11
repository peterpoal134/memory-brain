"""Source/clock/identity integration; synthetic local data, no model requests."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.config import Config

class SemanticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=BrainApp(Config(data_dir=self.tmp.name,owner='fixture',timezone='Asia/Shanghai',
            api_token='synthetic-semantic-token-0123456789'),start_worker=False)
        self.server=BrainServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.app.close();self.tmp.cleanup()
    def call(self,path,body=None,method=None,auth=True):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
        h={'Content-Type':'application/json'}
        if auth:h['Authorization']='Bearer '+self.app.config.api_token
        c.request(method or ('POST' if body is not None else 'GET'),path,None if body is None else json.dumps(body),h)
        response=c.getresponse();code=response.status;value=json.loads(response.read());c.close();return code,value
    def data(self,path,body=None,method=None):
        code,value=self.call(path,body,method);self.assertEqual(code,200,value);return value['data']
    def source(self,key,text,at='2026-01-07T23:45:00+08:00',conversation='c',role='user'):
        ack=self.app.store.ingest('fixture',conversation,key,role,text,recorded_at=at,metadata={'source_kind':'user' if role=='user' else 'model'})
        return self.app.store.get_message('fixture',ack['id'])
    def fact(self,source,metadata,text=None,start=0,end=None,disclosure='normal'):
        end=len(source['text']) if end is None else end
        return self.app.store.add_derived('fixture',source['conversation_id'],'fact',text or source['text'][start:end],
            [{'message_id':source['id'],'start':start,'end':end}],metadata=metadata,disclosure=disclosure)
    def test_old_flat_and_nested_claims_share_one_source_checked_conflict_contract(self):
        a=self.source('a','雪松项目地址在南岸。');b=self.source('b','雪松项目地址在北岸。')
        self.fact(a,{'subject':'雪松项目','attribute':'地址','value':'南岸'})
        self.fact(b,{'claim':{'subject':'雪松项目','predicate':'地址','value':'北岸','claims_verified':True}})
        result=self.app.recall({'conversation_id':'c','query':'雪松项目地址','token_budget':1800})
        self.assertEqual(result['conflicts'][0]['values'],['北岸','南岸'])
        self.assertFalse(result['conflicts'][0]['claims_verified'])
        claims=[a['metadata']['claim'] for a in result['assertions']]
        self.assertTrue(all(c['source_refs'] and c['source_roles']==['user'] and not c['claims_verified'] for c in claims))
    def test_relative_dates_use_each_original_clock_and_do_not_turn_plans_into_completed_events(self):
        original='设备明天打算送检。';s=self.source('plan',original)
        result=self.app.recall({'conversation_id':'c','query':'设备送检计划','token_budget':2000})
        self.assertIn('2026-01-08T00:00:00+08:00',result['block'])
        self.assertIn('不代表事件已发生',result['block']);self.assertIn(original,result['block'])
        self.assertEqual(self.app.store.get_message('fixture',s['id'])['text'],original)
        self.assertEqual(result['evidence'][0]['time_annotations'][0]['anchor_message_id'],s['id'])
    def test_time_scanner_does_not_read_a_neighboring_restricted_span(self):
        text='设备昨天到了。私密暗号后天变更。';s=self.source('privacy',text);split=text.index('私密')
        self.fact(s,{},start=0,end=split)
        self.fact(s,{'disclosure_topics':['私密暗号']},start=split,disclosure='only_if_asked')
        result=self.app.recall({'conversation_id':'c','query':'设备到了吗','token_budget':2000})
        self.assertIn('昨天',result['block']);self.assertNotIn('后天',result['block']);self.assertNotIn('私密暗号',result['block'])
        self.assertTrue(all(t['expression_end']<=split for e in result['evidence'] for t in e['time_annotations']))
    def test_existing_named_relationships_supply_only_bounded_navigation_candidates(self):
        a=self.source('student','林舟的导师是赵老师。');b=self.source('teacher','赵老师生日是五月八日。')
        self.fact(a,{'subject':'林舟','attribute':'导师','value':'赵老师'})
        self.fact(b,{'subject':'赵老师','attribute':'生日','value':'五月八日'})
        result=self.app.recall({'conversation_id':'c','query':'林舟导师的生日','token_budget':2000})
        self.assertGreaterEqual(result['trace']['routes']['entity_relation'],2)
        self.assertIn('五月八日',result['block']);self.assertFalse(result['trace']['relations']['truth_certified'])
    def test_http_cutoff_uses_archived_question_instead_of_later_wall_clock(self):
        old=self.source('old','雪松编号原来是十七。','2026-01-01T10:00:00+08:00')
        self.source('question','雪松编号多少？','2026-01-02T10:00:00+08:00')
        self.source('future','雪松编号已改二十。','2026-01-03T10:00:00+08:00')
        result=self.data('/v1/recall',{'conversation_id':'c','query':'雪松编号','current_message_id':'question','as_of':'2026-02-01T00:00:00+08:00'})
        self.assertIn('十七',result['block']);self.assertNotIn('二十',result['block'])
        self.assertTrue(all(e['recorded_at']<=old['recorded_at'] for e in result['evidence']))
        self.assertEqual(self.call('/v1/recall',{'conversation_id':'c','query':'雪松','as_of':'invalid'})[0],400)
    def test_profile_request_is_independent_of_slow_store_recall_and_has_its_own_budget(self):
        self.app.extensions.update_settings({'self_notes_enabled':True,'recall_enabled':False})
        self.app.extensions.update_self({'style_preferences':'用简短中文解释。'})
        self.data('/v1/standards',{'enabled':True,'add':{'id':'local','text':'不确定时明确说明。','priority':80}},'PATCH')
        self.data('/v1/standards',{'add':{'id':'other','text':'OTHER_CONVERSATION_RULE','scope':'conversation','conversation_id':'other'}},'PATCH')
        received=[]
        with patch.object(self.app.retriever,'recall',side_effect=AssertionError('profile cannot retrieve')):
            with self.app.store.lock:
                worker=threading.Thread(target=lambda:received.append(self.data('/v1/context/profile',{'conversation_id':'c'})))
                worker.start();worker.join(1);self.assertFalse(worker.is_alive())
        result=received[0];self.assertIn('用简短中文',result['block']);self.assertIn('不确定时',result['block'])
        self.assertNotIn('OTHER_CONVERSATION_RULE',result['block']);self.assertLessEqual(result['trace']['estimated_tokens'],384)
        self.assertEqual(self.call('/v1/context/profile',{'conversation_id':'c'},auth=False)[0],401)
        self.assertEqual(self.call('/v1/standards',auth=False)[0],401)
    def test_profile_material_is_not_mistaken_for_a_mutable_factual_record(self):
        self.data('/v1/standards',{'enabled':True,'add':{'id':'owner-rule','text':'请先给结论。'}},'PATCH')
        before=self.app.store.stats('fixture')
        one=self.data('/v1/context/profile',{'conversation_id':'c'});two=self.data('/v1/context/profile',{'conversation_id':'c'})
        self.assertEqual(one,two);self.assertEqual(self.app.store.stats('fixture'),before)
        self.assertEqual(self.data('/v1/standards')['version'],1)
        self.assertEqual(self.data('/v1/standards/owner-rule',{},'DELETE')['version'],2)
    def test_imported_note_cannot_become_an_attested_speaker_or_relative_date_anchor(self):
        ack=self.app.store.ingest('fixture','c','old-note','assistant','设备明天送检。',
            recorded_at='2026-01-07T12:00:00+08:00',metadata={'source_kind':'imported_memory','original_role':'unknown'})
        source=self.app.store.get_message('fixture',ack['id'])
        self.fact(source,{'subject':'设备','attribute':'送检','value':'明天','event_time':'明天'})
        result=self.app.recall({'conversation_id':'c','query':'设备送检','token_budget':2000})
        self.assertIn('旧库导入笔记',result['block']);self.assertNotIn('时间说明〔原话',result['block'])
        self.assertNotIn('2026-01-08',result['block'])
        self.assertTrue(all(not e['time_annotations'] for e in result['evidence']))
        self.assertTrue(all(a['metadata']['claim']['source_roles']==['unknown'] for a in result['assertions']))
    def test_same_timestamp_later_rows_and_batch_dependencies_cannot_cross_question_boundary(self):
        s=self.source('before','银杉设备编号是七。')
        question=self.source('question','银杉设备编号？')
        later=self.source('after','银杉设备编号现在二十一。')
        self.app.store.add_derived('fixture','c','fact','银杉设备后来的提炼索引',
            [{'message_id':s['id'],'start':0,'end':len(s['text'])}],dependency_message_ids=[s['id'],later['id']])
        # Enough later lexical matches would occupy a top-k unless the cutoff
        # is pushed into SQL before ranking, rather than merely post-filtered.
        for index in range(45):self.source('late-'+str(index),'银杉设备编号 二十一 银杉设备编号')
        result=self.data('/v1/recall',{'conversation_id':'c','query':'银杉设备编号','current_message_id':'question',
            'cutoff_message_id':999999,'as_of':'2026-12-31T00:00:00+08:00'})
        self.assertIn('编号是七',result['block']);self.assertNotIn('二十一',result['block'])
        self.assertEqual({r['message_id'] for e in result['evidence'] for r in e['source_refs']},{s['id']})
        self.assertFalse(result['assertions'])
