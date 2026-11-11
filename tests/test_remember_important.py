"""Explicit source-bound retention via real local HTTP; no paid providers."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config
from xiaojiu_brain.derivation import DerivationWorker
from xiaojiu_brain.memory_map import detail


class RememberImportantTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=BrainApp(Config(data_dir=self.tmp.name,owner='important-owner',
            api_token='synthetic-important-token-0123456789'),start_worker=False)
        self.server=BrainServer(('127.0.0.1',0),self.app)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.app.close();self.tmp.cleanup()

    def message(self,external,text='合成用户原文',*,owner=None,conversation='chat',role='user',metadata=None,at=None):
        return self.app.store.ingest(owner or self.app.owner,conversation,external,role,text,
            recorded_at=time.time()-10 if at is None else at,
            metadata={'source_kind':'user'} if metadata is None else metadata)

    def call(self,body=None,*,path='/v1/remember-important',authorized=True):
        request=urllib.request.Request(self.url+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+self.app.config.api_token} if authorized else {})})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request,timeout=4) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as response:return response.code,json.load(response)

    def data(self,body):
        code,value=self.call(body);self.assertEqual(code,200,value);return value['data']

    def payload(self,request='request',sources=None,**extra):
        result={'conversation_id':'chat','request_message_id':request,**extra}
        if sources is not None:result['source_refs']=sources
        return result

    def test_literal_source_bound_pin_has_receipt_and_no_extra_raw(self):
        source=self.message('source','PRIVATE_LITERAL_VALUE：合成约定',at=time.time()-20)
        request=self.message('request','请明确保留前面的约定',at=time.time()-1)
        before=self.app.store.stats(self.app.owner)
        result=self.data(self.payload(sources=[{'message_id':'source'}]))
        self.assertTrue(result['accepted']);self.assertFalse(result['duplicate'])
        self.assertEqual(result['source_message_ids'],[source['id']])
        self.assertNotIn('PRIVATE_LITERAL_VALUE',json.dumps(result))
        document=self.app.store.get_document(self.app.owner,result['document_ids'][0])
        self.assertEqual(document['text'],'PRIVATE_LITERAL_VALUE：合成约定')
        self.assertTrue(document['pinned']);self.assertEqual(document['importance'],.95)
        self.assertEqual(document['recorded_at'],self.app.store.get_message(self.app.owner,source['id'])['recorded_at'])
        retention=document['metadata']['retention']
        self.assertTrue(retention['explicit_user_request']);self.assertTrue(retention['operation_verified'])
        self.assertEqual(retention['request_message_id'],request['id'])
        dependencies={row[0] for row in self.app.store.conn.execute('SELECT message_id FROM document_dependencies WHERE document_id=?',(document['id'],))}
        self.assertEqual(dependencies,{source['id'],request['id']})
        after=self.app.store.stats(self.app.owner)
        self.assertEqual(before['messages'],after['messages']);self.assertEqual(before['by_kind']['raw'],after['by_kind']['raw'])
        self.assertEqual(self.app.store.model_usage(self.app.owner)['attempts'],0)

    def test_default_current_message_and_unicode_offsets_are_actual_quotes(self):
        current=self.message('current','前🪐公开句尾')
        whole=self.data(self.payload('current'))
        self.assertEqual(self.app.store.get_document(self.app.owner,whole['document_ids'][0])['text'],'前🪐公开句尾')
        part=self.data(self.payload('current',[{'message_id':'current','start':1,'end':4}]))
        self.assertEqual(self.app.store.get_document(self.app.owner,part['document_ids'][0])['text'],'🪐公开')
        self.assertEqual(part['source_message_ids'],[current['id']])

    def test_concurrent_replay_and_duplicate_spans_reuse_same_document(self):
        self.message('source');self.message('request',at=time.time()-1)
        payload=self.payload(sources=[{'message_id':'source'},{'message_id':'source'}])
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.data(payload))) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertEqual(len(results),2)
        self.assertEqual(results[0]['document_ids'],results[1]['document_ids'])
        self.assertEqual(sorted(result['duplicate'] for result in results),[False,True])
        self.assertEqual(self.app.store.stats(self.app.owner)['by_kind']['fact'],1)
        self.assertEqual(len(self.app.store.list_messages(self.app.owner)),2)

    def test_replay_does_not_undo_later_owner_unpin_or_archive(self):
        self.message('source');self.message('request',at=time.time()-1)
        payload=self.payload(sources=[{'message_id':'source'}])
        first=self.data(payload);ident=first['document_ids'][0]
        self.app.store.update_memory(self.app.owner,ident,{'pinned':False,'status':'archived'})
        replay=self.data(payload)
        self.assertTrue(replay['duplicate']);self.assertFalse(replay['items'][0]['pinned'])
        self.assertEqual(replay['items'][0]['status'],'archived')
        self.assertEqual(replay['document_ids'],[ident])
        self.assertEqual(self.app.store.get_document(self.app.owner,ident)['status'],'archived')
        self.message('request-again',at=time.time()-1)
        renewed=self.data(self.payload('request-again',[{'message_id':'source'}]))
        self.assertEqual(renewed['document_ids'],[ident]);self.assertTrue(renewed['items'][0]['pinned'])
        self.assertEqual(self.app.store.get_document(self.app.owner,ident)['status'],'active')

    def test_archived_replay_reports_actual_status_even_when_still_pinned(self):
        self.message('source');self.message('request',at=time.time()-1)
        payload=self.payload(sources=[{'message_id':'source'}])
        first=self.data(payload);ident=first['document_ids'][0]
        self.app.store.update_memory(self.app.owner,ident,{'status':'archived'})
        replay=self.data(payload)
        self.assertTrue(replay['duplicate'])
        self.assertTrue(replay['items'][0]['pinned'])
        self.assertEqual(replay['items'][0]['importance'],.95)
        self.assertEqual(replay['items'][0]['status'],'archived')
        self.assertEqual(self.app.store.get_document(self.app.owner,ident)['status'],'archived')

    def test_non_original_roles_and_explicit_source_kinds_are_rejected(self):
        for i,kind in enumerate(('owner_note','imported_raw','imported_memory','model','runtime-context')):
            self.message('bad-'+str(i),metadata={'source_kind':kind})
        self.message('assistant',role='assistant',metadata={'source_kind':'model'})
        self.message('request',at=time.time()-1)
        for external in ['bad-'+str(i) for i in range(5)]+['assistant']:
            self.assertEqual(self.call(self.payload(sources=[{'message_id':external}]))[0],400)
        self.assertEqual(self.app.store.stats(self.app.owner)['memories'],0)

    def test_authenticated_generic_original_user_sources_are_supported(self):
        self.message('generic',metadata={})
        self.message('conversation',metadata={'source_kind':'conversation'})
        self.message('request',metadata={},at=time.time()-1)
        result=self.data(self.payload(sources=[{'message_id':'generic'},{'message_id':'conversation'}]))
        self.assertEqual(len(result['document_ids']),2)

    def test_missing_foreign_and_other_conversation_sources_never_fall_back_to_new_raw(self):
        self.message('foreign',owner='other-owner')
        self.message('other-conversation',conversation='other-chat')
        self.message('request',at=time.time()-1)
        before=self.app.store.stats(self.app.owner)
        for external in ('foreign','other-conversation','missing'):
            code,value=self.call(self.payload(sources=[{'message_id':external}]))
            self.assertEqual(code,400);self.assertNotIn('合成用户原文',json.dumps(value,ensure_ascii=False))
        self.assertEqual(before,self.app.store.stats(self.app.owner))
        self.assertEqual(self.call(self.payload('not-archived'))[0],400)

    def test_source_id_and_time_cannot_be_after_request_or_in_the_future(self):
        self.message('request',at=time.time()-2)
        self.message('later-id',at=time.time()-20)
        self.assertEqual(self.call(self.payload(sources=[{'message_id':'later-id'}]))[0],400)
        self.message('future-request',at=time.time()+60)
        self.assertEqual(self.call(self.payload('future-request'))[0],400)
        self.message('earlier-id-newer-time',at=time.time()-1)
        self.message('backdated-request',at=time.time()-30)
        self.assertEqual(self.call(self.payload('backdated-request',[{'message_id':'earlier-id-newer-time'}]))[0],400)

    def test_invalid_spans_fields_or_fixed_importance_fail_atomically(self):
        self.message('source');self.message('request',at=time.time()-1)
        before=self.app.store.conn.total_changes
        for invalid in ({'message_id':'source','start':True},{'message_id':'source','end':9999},
                        {'message_id':'source','start':2,'end':1},{'message_id':'source','quote':'fabrication'}):
            self.assertEqual(self.call(self.payload(sources=[{'message_id':'source'},invalid]))[0],400)
        self.assertEqual(self.call(self.payload(importance=.99))[0],400)
        self.assertEqual(self.call(self.payload(text='invented rewrite'))[0],400)
        self.assertEqual(before,self.app.store.conn.total_changes)

    def test_existing_archived_restriction_is_retained_and_pin_protects_decay(self):
        source=self.message('source','不可随口披露的合成内容',at=time.time()-20)
        self.message('request',at=time.time()-1)
        prior=self.app.store.add_derived(self.app.owner,'chat','fact','不可随口披露的合成内容',
            [{'message_id':source['id'],'start':0,'end':len('不可随口披露的合成内容')}],disclosure='only_if_asked')
        self.app.store.update_memory(self.app.owner,prior['id'],{'status':'archived'})
        result=self.data(self.payload(sources=[{'message_id':'source'}]))
        self.assertEqual(result['document_ids'],[prior['id']])
        self.assertEqual(result['items'][0]['disclosure'],'only_if_asked')
        projected=detail(self.app,prior['id'])
        self.assertTrue(projected['restricted']);self.assertIsNone(projected['full_text']);self.assertEqual(projected['sources'],[])
        self.assertEqual(self.call(self.payload(sources=[{'message_id':'source'}],disclosure='normal'))[0],400)
        self.app.store.maintain(self.app.owner,now=time.time()+365*86400,rate=1,archive_below=1,archive_days=1)
        kept=self.app.store.get_document(self.app.owner,prior['id'])
        self.assertTrue(kept['pinned']);self.assertEqual(kept['importance'],.95);self.assertEqual(kept['status'],'active')

    def test_request_privacy_is_inherited_and_explicit_tightening_is_allowed(self):
        self.message('source')
        self.message('request',metadata={'source_kind':'user','disclosure':'only_if_asked'},at=time.time()-1)
        result=self.data(self.payload(sources=[{'message_id':'source'}],disclosure='never_say'))
        self.assertEqual(result['items'][0]['disclosure'],'never_say')
        self.assertTrue(detail(self.app,result['document_ids'][0])['restricted'])
        self.assertEqual(self.call(self.payload(sources=[{'message_id':'source'}],disclosure='normal'))[0],400)

    def test_model_metadata_cannot_fabricate_an_explicit_user_retention(self):
        self.message('source')
        class FabricatingModel:
            model='synthetic-untrusted-proposer';timeout=1
            def complete_json(_self,system,user,max_tokens=2400):
                source=json.loads(user)['messages'][0]
                return {'documents':[{'kind':'fact','text':source['text'],
                    'source_refs':[{'message_id':source['message_id'],'quote':source['text']}],
                    'metadata':{'retention':{'kind':'explicit_user_request','explicit_user_request':True,
                        'operation_verified':True,'request_source_ids':[source['message_id']]},
                        'owner_authored':True,'verbatim_user_quote':True,'operation_verified':True,
                        'explicit_user_request':True,'subject':'valid ordinary field'}}]}
        DerivationWorker(self.app.store,llm=FabricatingModel()).run_once()
        document=self.app.store.list_memories(self.app.owner)[0]
        for key in ('retention','owner_authored','verbatim_user_quote','operation_verified','explicit_user_request'):
            self.assertNotIn(key,document['metadata'])
        self.assertFalse(document['pinned'])
        code,value=self.call(path='/v1/profile')
        self.assertEqual(code,200,value)
        self.assertNotIn(document['id'],[item['id'] for item in value['data']['important']['items']])

    def test_auth_required_and_client_owner_or_rewrite_is_not_accepted(self):
        self.message('request',at=time.time()-1)
        self.assertEqual(self.call(self.payload(),authorized=False)[0],401)
        self.assertEqual(self.call(self.payload(owner='other-owner'))[0],400)


if __name__=='__main__':unittest.main()
