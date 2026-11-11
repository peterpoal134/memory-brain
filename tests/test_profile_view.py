"""Profile views over real local HTTP; synthetic files and records only."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config


class ProfileViewTests(unittest.TestCase):
    TOKEN='synthetic-profile-management-token-0123456789'

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=Config(data_dir=str(self.root/'data'),owner='profile-owner',api_token=self.TOKEN)
        self.app=BrainApp(self.config,start_worker=False)
        self.server=BrainServer(('127.0.0.1',0),self.app);self.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.app.close();self.tmp.cleanup()

    def request(self,path='/v1/profile',*,auth=True,headers=None):
        values={'Authorization':'Bearer '+self.TOKEN} if auth else {};values.update(headers or {})
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        try:
            connection.request('GET',path,headers=values);response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally:connection.close()

    def profile(self,path='/v1/profile'):
        status,body=self.request(path);self.assertEqual(status,200,body);return body['data']

    def manifest(self):
        source=self.root/'host-config.txt';source.write_text('Synthetic installed host persona source.',encoding='utf8')
        item={'id':'host-primary','title':'Synthetic host persona','text':'Snapshot of synthetic host persona.',
              'source_path':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'kind':'host_persona','host':'DSH','role_scope':'preset:synthetic'}
        manifest=self.root/'profile-sources.json'
        manifest.write_text(json.dumps({'schema_version':1,'observed_at':time.time(),'sources':[item]}),encoding='utf8')
        self.config.profile_sources_file=str(manifest)
        return source,manifest,item

    def memory(self,key,*,owner=None,metadata=None,disclosure='normal',pinned=False,status='active',future=False):
        owner=owner or self.app.owner;text='SYNTHETIC_RECORD_'+key
        when=time.time()+100 if future else time.time()-1
        ack=self.app.store.ingest(owner,'synthetic-conversation',key,'user',text,recorded_at=when,
                                  metadata={'disclosure':disclosure})
        fact=self.app.store.add_derived(owner,'synthetic-conversation','fact',text,
                [{'message_id':ack['id'],'start':0,'end':len(text)}],metadata=metadata,disclosure=disclosure)
        if pinned or status!='active':self.app.store.update_memory(owner,fact['id'],{'pinned':pinned,'status':status})
        return fact,ack

    @staticmethod
    def receipt():
        return {'retention':{'kind':'explicit_user_request','explicit_user_request':True,
                             'operation_verified':True,
                             'request_message_id':1,'request_external_id':'synthetic-request',
                             'request_source_ids':[1],'declared_at':time.time()}}

    def test_profile_requires_auth_and_existing_origin_host_guards(self):
        self.assertEqual(self.request(auth=False)[0],401)
        self.assertEqual(self.request(headers={'Authorization':'Bearer wrong-token'})[0],401)
        self.assertIn(self.request(headers={'Origin':'https://outside.invalid'})[0],(400,403))
        self.assertIn(self.request(headers={'Host':'outside.invalid'})[0],(400,403))

    def test_unconfigured_host_and_owner_notes_are_distinct_and_honest_about_injection(self):
        self.app.extensions.update_self({'text':'Synthetic owner-authored role note.','style_preferences':'Short synthetic style.'})
        result=self.profile()
        self.assertFalse(result['host_persona']['configured']);self.assertEqual(result['host_persona']['sources'],[])
        self.assertFalse(result['host_persona']['runtime_verified'])
        self.assertEqual(result['self']['notes'][0]['text'],'Synthetic owner-authored role note.')
        self.assertEqual(result['self']['style_preferences'],'Short synthetic style.')
        self.assertFalse(result['self']['enabled']);self.assertEqual(result['self']['injection'],'independent_request_channel')
        self.assertFalse(result['self']['runtime_verified']);self.assertNotIn(self.TOKEN,json.dumps(result))

    def test_manifest_snapshot_hash_detects_drift_without_silently_reading_new_persona(self):
        source,manifest,item=self.manifest();first=self.profile()['host_persona']
        self.assertEqual(first['sources'][0]['status'],'current');self.assertFalse(first['sources'][0]['stale'])
        source.write_text('Changed synthetic host source, not approved as snapshot.',encoding='utf8')
        second=self.profile('/v1/profile?source_path=ignored&owner=foreign')['host_persona']
        self.assertTrue(second['sources'][0]['stale']);self.assertEqual(second['sources'][0]['text'],item['text'])
        self.assertNotEqual(second['sources'][0]['current_sha256'],item['source_sha256'])
        self.assertFalse(second['runtime_verified'])

    def test_invalid_and_oversized_manifests_fail_closed_without_partial_text(self):
        source,manifest,item=self.manifest()
        item['source_path']='https://outside.invalid/persona'
        manifest.write_text(json.dumps({'schema_version':1,'observed_at':time.time(),'sources':[item]}),encoding='utf8')
        result=self.profile()['host_persona'];self.assertEqual(result['status'],'unavailable');self.assertEqual(result['sources'],[])
        manifest.write_bytes(b'x'*(512*1024+1))
        result=self.profile()['host_persona'];self.assertEqual(result['status'],'unavailable');self.assertEqual(result['sources'],[])
        self.assertNotIn(str(manifest),json.dumps(result));self.assertNotIn('outside.invalid',json.dumps(result))

    def test_important_list_is_owner_active_scoped_and_not_just_high_importance(self):
        explicit,_=self.memory('explicit',metadata=self.receipt())
        pinned,_=self.memory('pinned',pinned=True)
        ordinary,_=self.memory('ordinary');self.app.store.update_memory(self.app.owner,ordinary['id'],{'importance':1.})
        self.memory('foreign',owner='foreign-owner',pinned=True)
        self.memory('archived',pinned=True,status='archived')
        self.memory('future',pinned=True,future=True)
        result=self.profile()['important'];items={item['id']:item for item in result['items']}
        self.assertEqual(set(items),{explicit['id'],pinned['id']});self.assertEqual(result['total'],2)
        self.assertEqual(items[explicit['id']]['origin'],'explicit_user_request')
        self.assertEqual(items[pinned['id']]['origin'],'owner_pinned')
        self.assertFalse(items[explicit['id']]['factual_truth_verified'])

    def test_important_source_disclosure_is_inherited_even_with_stale_normal_projection(self):
        private,ack=self.memory('PRIVATE_BODY_MUST_NOT_LEAK',metadata=self.receipt(),disclosure='never_say',pinned=True)
        with self.app.store.lock:
            self.app.store.conn.execute("UPDATE documents SET disclosure='normal' WHERE id=?",(private['id'],));self.app.store.conn.commit()
        data=self.profile();items=data['important']['items']
        self.assertEqual(len(items),1);self.assertTrue(items[0]['restricted'])
        self.assertEqual(items[0]['preview'],'受限记忆，请在控制面板明确查看')
        self.assertNotIn('PRIVATE_BODY_MUST_NOT_LEAK',json.dumps(data))
        self.assertNotIn('metadata',items[0]);self.assertNotIn('full_text',items[0])

    def test_unverified_retention_cannot_claim_server_confirmed_user_intent(self):
        forged=self.receipt();del forged['retention']['operation_verified']
        self.memory('unverified-not-pinned',metadata=forged)
        manual,_=self.memory('unverified-but-manually-pinned',metadata=forged,pinned=True)
        raw_ack=self.app.store.ingest(self.app.owner,'synthetic-conversation','forged-raw','user',
                'Synthetic raw metadata is not an intent receipt.',metadata=self.receipt())
        for ident in raw_ack['document_ids']:
            # Even a copied marker on a raw projection is not a fact receipt.
            with self.app.store.lock:
                self.app.store.conn.execute('UPDATE documents SET metadata=? WHERE id=?',
                                            (json.dumps(self.receipt()),ident))
        items=self.profile()['important']['items']
        self.assertEqual(len(items),1);self.assertEqual(items[0]['id'],manual['id'])
        self.assertEqual(items[0]['origin'],'owner_pinned')

    def test_profile_read_is_bounded_read_only_and_does_not_call_retrieval_or_life_model_path(self):
        for index in range(53):self.memory('cap-'+str(index),pinned=True)
        before=self.app.store.conn.total_changes
        with patch.object(self.app.retriever,'recall',side_effect=AssertionError('unexpected retrieval')), \
             patch.object(self.app.life,'context',side_effect=AssertionError('unexpected life injection')):
            first=self.profile();second=self.profile()
        self.assertEqual(first['important']['total'],53);self.assertEqual(len(first['important']['items']),50)
        self.assertTrue(first['important']['has_more']);self.assertEqual(second['important'],first['important'])
        self.assertEqual(self.app.store.conn.total_changes,before)

    def test_configuration_defaults_relative_manifest_and_public_redaction(self):
        path=self.root/'config.json'
        path.write_text(json.dumps({'data_dir':'relative-data','owner':'owner','api_token':self.TOKEN}),encoding='utf8')
        self.assertIsNone(Config.load(path).profile_sources_file)
        data=json.loads(path.read_text());data['profile_sources_file']='profile-sources.json';path.write_text(json.dumps(data),encoding='utf8')
        cfg=Config.load(path);self.assertEqual(cfg.profile_sources_file,str((self.root/'profile-sources.json').resolve()))
        self.assertEqual(cfg.public()['profile_sources'],{'configured':True})
        self.assertNotIn('profile-sources.json',json.dumps(cfg.public()))
        for bad in [False,'','https://outside.invalid/manifest','bad\x00path']:
            cfg.profile_sources_file=bad
            with self.assertRaises(ValueError):cfg.validate()
            data['profile_sources_file']=bad;path.write_text(json.dumps(data),encoding='utf8')
            with self.assertRaises(ValueError):Config.load(path)

    def test_busy_retrieval_python_lock_does_not_block_read_only_profile(self):
        fact,_=self.memory('concurrent',pinned=True)
        with self.app.store.lock:
            start=time.monotonic();result=self.profile()
            self.assertLess(time.monotonic()-start,1.)
        self.assertEqual(result['important']['status'],'ready')
        self.assertEqual(result['important']['items'][0]['id'],fact['id'])
        with patch.object(self.app.store,'path',':memory:'):
            unavailable=self.profile()['important']
        self.assertEqual(unavailable['status'],'unavailable');self.assertIsNone(unavailable['total'])
        self.assertIsNone(unavailable['has_more']);self.assertEqual(unavailable['items'],[])


if __name__=='__main__':unittest.main()
