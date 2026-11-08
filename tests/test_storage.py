import concurrent.futures
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from xiaojiu_brain.storage import Store, JobLeaseLost


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'brain.db'
        self.store=Store(self.path)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def add(self,text='钥匙放在蓝盒里',owner='owner',conversation='chat',external='m1',**kwargs):
        return self.store.ingest(owner,conversation,external,'user',text,**kwargs)

    def ref(self,row,start=0,end=None):
        message=self.store.get_message('owner',row['id'])
        return {'message_id':row['id'],'start':start,'end':len(message['text']) if end is None else end}

    def test_original_available_immediately_without_any_model(self):
        row=self.add('我的常用网名是远帆，表格上就这么填。')
        results=self.store.lexical_search('owner','远帆','chat')
        self.assertTrue(results)
        self.assertEqual(results[0]['source_refs'][0]['message_id'],row['id'])
        self.assertEqual(results[0]['source_refs'][0]['quote'],'我的常用网名是远帆，表格上就这么填。')
        self.assertEqual(self.store.job_stats()['pending'],2)

    def test_concurrent_replay_is_idempotent_and_body_collision_rejected(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.add(),range(24)))
        self.assertEqual(sum(r['accepted'] for r in results),1)
        self.assertEqual(sum(r['duplicate'] for r in results),23)
        self.assertEqual(self.store.stats('owner')['messages'],1)
        self.assertEqual(self.store.job_stats()['total'],2)
        with self.assertRaises(ValueError):self.add('钥匙后来换了地方')

    def test_one_transaction_covers_key_message_fts_and_jobs(self):
        original=self.store._enqueue
        def fail(*args,**kwargs):raise OSError('simulated queue disk failure')
        self.store._enqueue=fail
        with self.assertRaises(OSError):self.add()
        self.store._enqueue=original
        self.assertEqual(self.store.stats('owner')['messages'],0)
        self.assertEqual(self.store.lexical_search('owner','钥匙'),[])
        self.assertEqual(self.store.job_stats()['total'],0)
        self.assertTrue(self.add()['accepted'])

    def test_owner_conversation_and_time_filter_before_top_k(self):
        self.add('虹膜索引 属于我的旧记录',recorded_at=10)
        for n in range(20):self.add('虹膜索引 虹膜索引 虹膜索引',owner='other',external=str(n),recorded_at=11)
        self.add('虹膜索引 另一会话',conversation='other-chat',external='other',recorded_at=11)
        self.add('虹膜索引 尚未发生',external='future',recorded_at=100)
        found=self.store.lexical_search('owner','虹膜索引','chat',limit=1,as_of=20)
        self.assertEqual(len(found),1);self.assertEqual(found[0]['text'],'虹膜索引 属于我的旧记录')
        self.assertTrue(all(d['owner']=='owner' for d in self.store.documents('owner')))

    def test_same_external_id_is_independent_between_conversations_and_roles(self):
        one=self.add();two=self.add(conversation='second')
        three=self.store.ingest('owner','chat','m1','assistant','我会提醒你')
        self.assertEqual(len({one['id'],two['id'],three['id']}),3)
        self.assertEqual(self.store.find_message('owner','chat','m1')['id'],one['id'])
        self.assertIsNone(self.store.find_message('other','chat','m1'))
        self.assertIsNone(self.store.get_message('other',one['id']))

    def test_long_messages_have_exact_overlapping_spans(self):
        text='开头。'+('这是一段完整的原话，不能由摘要代替。'*100)+'末尾唯一记号'
        row=self.add(text)
        docs=self.store.documents('owner','chat')
        self.assertGreater(len(docs),1)
        for doc in docs:
            ref=doc['source_refs'][0]
            self.assertEqual(doc['text'],text[ref['start']:ref['end']])
        self.assertTrue(any('末尾唯一记号' in d['text'] for d in self.store.lexical_search('owner','末尾唯一记号','chat')))
        self.assertEqual(len(self.store.list_messages('owner')),1)

    def test_spans_are_scope_checked_and_quotes_are_computed(self):
        row=self.add('柜子里有一本绿皮书')
        forged={**self.ref(row),'quote':'伪造的话'}
        d=self.store.add_derived('owner','chat','fact','有一本绿皮书',[forged])
        self.assertEqual(d['source_refs'][0]['quote'],'柜子里有一本绿皮书')
        self.assertEqual(d['metadata']['verification'],'source_spans_validated_not_semantic_proof')
        for owner,conversation,ref in [('other','chat',self.ref(row)),('owner','other',self.ref(row)),('owner','chat',self.ref(row,end=999))]:
            with self.assertRaises(ValueError):self.store.add_derived(owner,conversation,'fact','提议文本',[ref])

    def test_identical_content_from_distinct_sources_is_not_merged(self):
        a=self.add('今天要买苹果');b=self.add('今天要买苹果',external='m2')
        x=self.store.add_derived('owner','chat','fact','要买苹果',[self.ref(a)])
        y=self.store.add_derived('owner','chat','fact','要买苹果',[self.ref(b)])
        z=self.store.add_derived('owner','chat','fact','要买苹果',[self.ref(a)])
        self.assertNotEqual(x['id'],y['id']);self.assertEqual(x['id'],z['id'])
        self.assertEqual(len(self.store.list_memories('owner')),2)

    def test_corrections_are_retained_as_separate_source_claims(self):
        a=self.add('联络人叫陈霖');b=self.add('刚才说错了，联络人叫陈林',external='m2')
        for row,text in [(a,'联络人叫陈霖'),(b,'联络人叫陈林')]:
            self.store.add_derived('owner','chat','fact',text,[self.ref(row)],metadata={'subject':'联络人','attribute':'名字'})
        self.assertEqual(len(self.store.list_memories('owner')),2)
        self.assertEqual(len(self.store.lexical_search('owner','联络人','chat',kind='derived')),2)

    def test_vectors_and_derived_indexes_deleted_with_original(self):
        a=self.add();b=self.add('另一本册子放在门边',external='m2')
        d=self.store.add_derived('owner','chat','summary','钥匙和册子的地点',[self.ref(a),self.ref(b)])
        self.store.set_vector('owner',d['id'],[.2,.5],'test-v1')
        self.assertEqual(self.store.stats('owner')['vectors'],1)
        self.assertFalse(self.store.delete_message('other',a['id'])['deleted'])
        self.store.delete_message('owner',a['id'])
        self.assertIsNone(self.store.get_document('owner',d['id']))
        self.assertEqual(self.store.stats('owner')['vectors'],0)
        self.assertFalse(self.store.lexical_search('owner','钥匙','chat'))
        self.assertIsNotNone(self.store.get_message('owner',b['id']))
        replay=self.add();self.assertTrue(replay['duplicate']);self.assertTrue(replay['deleted'])
        self.assertEqual(self.store.stats('owner')['messages'],1)

    def test_vector_validation_and_owner_protection(self):
        row=self.add();did=row['document_ids'][0]
        for vector in [[float('nan')],[float('inf')],[0,0],['.2'],[True]]:
            with self.assertRaises(ValueError):self.store.set_vector('owner',did,vector,'v1')
        with self.assertRaises(ValueError):self.store.set_vector('other',did,[1,0],'v1')
        self.store.set_vector('owner',did,[1,.5],'v1')
        self.assertEqual(self.store.get_document('owner',did)['vector_model'],'v1')

    def test_mutation_cannot_change_original_words_or_weaken_privacy(self):
        row=self.add('项目暗号是六棱',metadata={'disclosure':'only_if_asked','disclosure_topics':['项目暗号']})
        derived=self.store.add_derived('owner','chat','fact','项目暗号是六棱',[self.ref(row)],metadata={'disclosure_topics':['天气']})
        self.assertEqual(derived['disclosure'],'only_if_asked')
        self.assertEqual(derived['metadata']['disclosure_topics'],['项目暗号'])
        with self.assertRaises(ValueError):self.store.update_memory('owner',derived['id'],{'text':'改写'})
        with self.assertRaises(ValueError):self.store.update_memory('owner',derived['id'],{'disclosure':'normal'})
        self.store.update_memory('owner',row['document_ids'][0],{'disclosure':'never_say'})
        self.assertEqual(self.store.get_document('owner',derived['id'])['disclosure'],'never_say')

    def test_multi_source_topics_intersection_is_conservative(self):
        a=self.add('工资记录甲',metadata={'disclosure':'only_if_asked','disclosure_topics':['工资']})
        b=self.add('病历记录乙',external='m2',metadata={'disclosure':'only_if_asked','disclosure_topics':['健康']})
        d=self.store.add_derived('owner','chat','summary','两个个人记录',[self.ref(a),self.ref(b)])
        self.assertEqual(d['metadata']['disclosure_topics'],[])
        self.assertEqual(d['disclosure'],'only_if_asked')

    def test_idempotent_derived_content_cannot_discard_stricter_policy(self):
        row=self.add('记录内容')
        first=self.store.add_derived('owner','chat','fact','记录内容',[self.ref(row)])
        strict=self.store.add_derived('owner','chat','fact','记录内容',[self.ref(row)],disclosure='never_say')
        normal=self.store.add_derived('owner','chat','fact','记录内容',[self.ref(row)])
        self.assertEqual(first['id'],strict['id']);self.assertEqual(strict['id'],normal['id'])
        self.assertEqual(normal['disclosure'],'never_say')

    def test_timestamps_preserve_source_offset_and_override_spoofed_metadata(self):
        r=self.add(recorded_at='2026-06-10T18:30:00+08:00',metadata={'recorded_at_iso':'1999-01-01T00:00:00Z'})
        msg=self.store.get_message('owner',r['id'])
        self.assertEqual(msg['metadata']['recorded_at_iso'],'2026-06-10T18:30:00+08:00')
        zero=self.add(external='epoch',recorded_at=0)
        self.assertEqual(self.store.get_message('owner',zero['id'])['recorded_at'],0)

    def test_archive_is_a_read_option_not_deletion(self):
        row=self.add();did=row['document_ids'][0]
        self.store.update_memory('owner',did,{'status':'archived','pinned':True,'importance':.8})
        self.assertFalse(self.store.lexical_search('owner','钥匙','chat'))
        self.assertEqual(len(self.store.lexical_search('owner','钥匙','chat',include_archived=True,kind='raw')),1)
        self.assertEqual(self.store.get_message('owner',row['id'])['text'],'钥匙放在蓝盒里')

    def test_wal_backup_and_owner_export(self):
        self.add();self.add('别人的信息',owner='other')
        result=self.store.backup(Path(self.tmp.name)/'snapshot.db')
        self.assertEqual(result['integrity'],'ok')
        check=sqlite3.connect(result['path'])
        try:self.assertEqual(check.execute('select count(*) from messages').fetchone()[0],2)
        finally:check.close()
        exported=self.store.export_json('owner')
        self.assertEqual(len(exported['messages']),1)
        self.assertTrue(all(d['owner']=='owner' for d in exported['documents']))
        with self.assertRaises(ValueError):self.store.backup(self.path)

    def test_maintenance_is_elapsed_time_based_and_idempotent(self):
        row=self.add(recorded_at=0)
        d=self.store.add_derived('owner','chat','fact','钥匙放在蓝盒里',[self.ref(row)],importance=.5)
        self.store.maintain('owner',now=86400,rate=.1,archive_days=100)
        first=self.store.get_document('owner',d['id'])['importance']
        self.assertAlmostEqual(first,.5*math.exp(-.1))
        again=self.store.maintain('owner',now=86400,rate=.1,archive_days=100)
        self.assertEqual(again['decayed'],0)
        self.assertEqual(first,self.store.get_document('owner',d['id'])['importance'])
        self.store.maintain('owner',now=172800,rate=.1,archive_days=100)
        self.assertAlmostEqual(self.store.get_document('owner',d['id'])['importance'],.5*math.exp(-.2))

    def test_maintenance_protects_raw_and_pinned_and_archives_only_old_derived(self):
        row=self.add(recorded_at=0,metadata={'importance':.05})
        one=self.store.add_derived('owner','chat','fact','低分派生',[self.ref(row)],importance=.05)
        two=self.store.add_derived('owner','chat','summary','用户钉住的派生',[self.ref(row)],importance=.05)
        self.store.update_memory('owner',two['id'],{'pinned':True})
        r=self.store.maintain('owner',now=100*86400)
        self.assertEqual(r['archived'],1);self.assertEqual(r['deleted_raw'],0)
        self.assertEqual(self.store.get_document('owner',one['id'])['status'],'archived')
        self.assertEqual(self.store.get_document('owner',two['id'])['status'],'active')
        self.assertEqual(self.store.get_document('owner',two['id'])['importance'],.05)
        self.assertEqual(self.store.get_document('owner',row['document_ids'][0])['importance'],.05)
        self.assertEqual(self.store.get_message('owner',row['id'])['text'],'钥匙放在蓝盒里')

    def test_maintenance_is_owner_scoped_and_validates_parameters(self):
        row=self.add(recorded_at=0)
        d=self.store.add_derived('owner','chat','fact','原始提议',[self.ref(row)],importance=.5)
        self.store.maintain('someone-else',now=100*86400)
        self.assertEqual(self.store.get_document('owner',d['id'])['importance'],.5)
        for patch in [{'rate':float('nan')},{'rate':-1},{'archive_below':2},{'archive_days':-5},{'now':float('inf')}]:
            with self.assertRaises(ValueError):self.store.maintain('owner',**patch)

    def test_live_embedding_precedes_import_backlog_with_fifo_inside_classes(self):
        imported=[]
        for i in range(3):
            old=self.add('历史原话'+str(i),external='old-'+str(i),
                metadata={'source_kind':'imported_raw','import_model_policy':'local_only','skip_derivation':True})
            imported.extend(old['document_ids'])
        fresh=self.add('新对话必须立即可检索',external='live')
        claimed=self.store.claim_jobs('embed',limit=4,worker_id='priority-test')
        self.assertEqual([j['document_id'] for j in claimed],fresh['document_ids']+imported)
        derive=self.store.claim_jobs('derive',limit=10,worker_id='derive-test')
        self.assertEqual([j['message_id'] for j in derive],[fresh['id']])


if __name__=='__main__':unittest.main()
