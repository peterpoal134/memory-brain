from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest

from xiaojiu_brain.storage import Store
from xiaojiu_brain.derivation import DerivationWorker
from xiaojiu_brain.migration import inspect_legacy,import_legacy


class RemoteEmbeddingStub:
    model='remote-stub';url='https://external.invalid/embeddings';timeout=1
    def __init__(self):self.calls=0
    def embed_many(self,texts):self.calls+=1;return [[1.,.5] for _ in texts]


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.legacy=self.root/'legacy.db';self.destination=self.root/'new.db'
        c=sqlite3.connect(self.legacy)
        c.executescript('''
          CREATE TABLE raw_messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,sender TEXT,text TEXT,created_at REAL,turn_id TEXT,metadata TEXT);
          CREATE TABLE memories(id INTEGER PRIMARY KEY,kind TEXT,text TEXT,importance REAL,created_at REAL,visibility TEXT,source TEXT,provenance TEXT,source_msg_id INTEGER,meta TEXT,status TEXT);
        ''')
        raw=[(1,'chat-A','user','person','钥匙原来放蓝盒',100,'external-1','{}'),
             (2,'chat-A','assistant','person','我会提醒你核对盒子',101,'external-1','{}'),
             (3,'chat-B','user','person','另一个会话的记录',102,'external-1','{}'),
             (4,'chat-A','user','person','密钥备忘不能公开',103,'secret-message',json.dumps({'disclosure':'never_say','disclosure_topics':['密钥']})),
             (5,'chat-A','tool','person','不是可见终稿',104,'tool','{}')]
        c.executemany('INSERT INTO raw_messages VALUES(?,?,?,?,?,?,?,?)',raw)
        memories=[(10,'episode','钥匙和提醒的历史摘要',.25,101,'public','digest','',2,json.dumps({'session':'chat-A','msg_ids':[1,2]}),'active'),
                  (11,'fact','没有原话来源的旧事实',.7,50,'private','http','written',0,'{}','active'),
                  (12,'preference','旧档案中的保密偏好',.6,103,'private','digest','',4,json.dumps({'disclosure':'never_say','msg_ids':[4]}),'archived'),
                  (13,'episode','只有最后ID而没有可验证来源的摘要',.25,101,'public','digest','',2,json.dumps({'session':'chat-A'}),'active')]
        c.executemany('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)',memories);c.commit();c.close()
        self.before=hashlib.sha256(self.legacy.read_bytes()).hexdigest()
        self.store=Store(self.destination)

    def tearDown(self):self.store.close();self.temp.cleanup()

    def test_inspection_and_default_dry_run_do_not_copy_body_or_mutate_destination(self):
        inspected=inspect_legacy(self.legacy)
        self.assertFalse(inspected['includes_message_bodies'])
        self.assertEqual({r['name']:r['rows'] for r in inspected['tables']},{'memories':4,'raw_messages':5})
        self.assertNotIn('钥匙',json.dumps(inspected,ensure_ascii=False))
        report=import_legacy(self.store,self.legacy,'owner')
        self.assertTrue(report['dry_run']);self.assertEqual(report['raw_eligible'],4)
        self.assertEqual(report['raw_unsupported_role'],1)
        self.assertEqual(self.store.stats('owner')['messages'],0)
        self.assertEqual(self.store.job_stats()['total'],0)
        self.assertEqual(hashlib.sha256(self.legacy.read_bytes()).hexdigest(),self.before)

    def test_complete_import_preserves_sources_scope_and_unknown_historical_notes(self):
        result=import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        self.assertTrue(result['ok'],result['errors']);self.assertEqual(result['raw_imported'],4)
        self.assertEqual(result['memory_notes_imported'],4);self.assertEqual(result['linked_memories'],2)
        messages=self.store.list_messages('owner',limit=100)
        self.assertEqual(len(messages),8)
        originals=[m for m in messages if m['metadata']['source_kind']=='imported_raw']
        self.assertEqual({m['conversation_id'] for m in originals},{'chat-A','chat-B'})
        self.assertEqual([m['metadata']['original_turn_id'] for m in originals if m['metadata']['original_id']==1],['external-1'])
        notes=[m for m in messages if m['metadata']['source_kind']=='imported_memory']
        self.assertTrue(all(m['role']=='assistant' and m['metadata']['original_role']=='unknown' for m in notes))
        self.assertTrue(all(not m['metadata']['is_original_conversation_message'] for m in notes))
        unlinked=next(m for m in notes if m['metadata']['original_id']==11)
        self.assertEqual(unlinked['text'],'没有原话来源的旧事实')
        self.assertEqual(unlinked['metadata']['original_provenance'],'written')
        self.assertEqual(unlinked['metadata']['disclosure'],'only_if_asked')
        for d in self.store.list_memories('owner',limit=100):
            self.assertTrue(all(self.store.get_message('owner',ref['message_id'])['metadata']['source_kind']=='imported_raw' for ref in d['source_refs']))
            self.assertEqual(d['metadata']['import_model_policy'],'local_only')
        stats=self.store.stats('owner')
        self.assertEqual((stats['messages'],stats['original_messages'],stats['imported_notes'],stats['owner_notes']),(8,4,4,0))
        self.store.ingest('owner','chat-A','owner-authored-note','user','合成的主人笔记',
            metadata={'source_kind':'owner_note','skip_derivation':True})
        stats=self.store.stats('owner')
        self.assertEqual((stats['messages'],stats['original_messages'],stats['imported_notes'],stats['owner_notes']),(9,4,4,1))

    def test_repeated_import_is_idempotent_and_original_hash_stays_the_same(self):
        one=import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        before=self.store.stats('owner');jobs=self.store.job_stats()['total']
        two=import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        self.assertTrue(one['ok'] and two['ok'])
        self.assertEqual(two['raw_imported'],0);self.assertEqual(two['raw_duplicates'],4)
        self.assertEqual(two['memory_notes_imported'],0);self.assertEqual(two['memory_note_duplicates'],4)
        self.assertEqual(self.store.stats('owner'),before);self.assertEqual(self.store.job_stats()['total'],jobs)
        self.assertTrue(two['source_main_hash_stable'])
        self.assertEqual(hashlib.sha256(self.legacy.read_bytes()).hexdigest(),self.before)

    def test_no_automatic_cloud_model_reprocessing_and_secret_policy_is_preserved(self):
        import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        self.assertNotIn('derive',self.store.job_stats()['by_type'])
        self.assertTrue(all(m['metadata']['derivation_status']=='skipped_import' for m in self.store.list_messages('owner',limit=100)))
        remote=RemoteEmbeddingStub();DerivationWorker(self.store,embedder=remote).run_once()
        self.assertEqual(remote.calls,0)
        self.assertEqual(self.store.job_stats()['blocked_policy'],self.store.job_stats()['total'])
        never=[d for d in self.store.documents('owner',include_archived=True) if d['metadata'].get('original_id')==12]
        self.assertTrue(never);self.assertTrue(all(d['disclosure']=='never_say' for d in never))

    def test_owner_private_compatibility_keeps_explicit_disclosure_rules(self):
        source=sqlite3.connect(self.legacy)
        try:
            source.execute("UPDATE memories SET visibility='private',meta=? WHERE id=13",
                (json.dumps({'disclosure':'only_if_asked','disclosure_topics':['提醒']}),))
            source.commit()
        finally:source.close()
        result=import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='owner-private',private_notes_policy='owner')
        self.assertTrue(result['ok'],result['errors']);self.assertEqual(result['private_notes_policy'],'owner')
        self.assertEqual(result['owner_scoped_private_notes'],1)
        notes={r['metadata']['original_id']:r for r in self.store.list_messages('owner',limit=100)
            if r['metadata'].get('source_kind')=='imported_memory'}
        self.assertEqual(notes[11]['metadata']['disclosure'],'normal')
        self.assertEqual(notes[12]['metadata']['disclosure'],'never_say')
        self.assertEqual(notes[13]['metadata']['disclosure'],'only_if_asked')
        self.assertEqual(notes[13]['metadata']['disclosure_topics'],['提醒'])
        self.assertTrue(all(r['metadata']['legacy_private_notes_policy']=='owner' for r in notes.values()))
        self.assertTrue(all(r['metadata']['import_model_policy']=='local_only' for r in notes.values()))
        self.assertEqual(notes[11]['metadata']['original_visibility'],'private')
        from scripts.verify_migration import verify_migration
        verified=verify_migration(self.legacy,self.destination,'owner','owner-private',
            ['chat-A','chat-B'],private_notes_policy='owner')
        self.assertTrue(verified['ok'],verified['problems'])

    def test_conflicting_destination_key_is_reported_without_overwriting(self):
        self.store.ingest('owner','chat-A','legacy:fixture:raw:1','user','新库自己的不同内容')
        report=import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        self.assertFalse(report['ok']);self.assertTrue(report['errors'])
        self.assertEqual(self.store.find_message('owner','chat-A','legacy:fixture:raw:1')['text'],'新库自己的不同内容')

    def test_live_wal_commits_are_included_by_sqlite_backup(self):
        source=sqlite3.connect(self.legacy);source.execute('PRAGMA journal_mode=WAL')
        source.execute("INSERT INTO raw_messages VALUES(6,'chat-C','user','person','仅在WAL中的新记录',105,'wal-message','{}')");source.commit()
        try:
            result=import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='wal-fixture')
            self.assertEqual(result['raw_imported'],5)
            self.assertTrue(self.store.lexical_search('owner','WAL中的新记录','chat-C'))
        finally:source.close()

    def test_imported_source_local_only_policy_is_inherited_by_new_projection(self):
        import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        msg=self.store.find_message('owner','chat-A','legacy:fixture:raw:1')
        d=self.store.add_derived('owner','chat-A','fact','新派生',[{'message_id':msg['id'],'start':0,'end':len(msg['text'])}])
        self.assertEqual(d['metadata']['import_model_policy'],'local_only')

    def test_extra_legacy_columns_are_preserved_without_becoming_new_assertions(self):
        source=sqlite3.connect(self.legacy)
        source.execute('ALTER TABLE memories ADD COLUMN fact_time REAL')
        source.execute('UPDATE memories SET fact_time=12345 WHERE id=11');source.commit();source.close()
        import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        note=next(m for m in self.store.list_messages('owner',limit=100) if m['metadata'].get('source_kind')=='imported_memory' and m['metadata']['original_id']==11)
        self.assertEqual(note['metadata']['legacy_record']['fact_time'],12345)
        self.assertTrue(note['metadata']['source_uncertain'])

    def test_same_source_destination_is_rejected_before_reading(self):
        with self.assertRaises(ValueError):import_legacy(self.store,self.destination,'owner',apply=True)

    def test_empty_or_unrelated_database_is_rejected_without_target_writes(self):
        empty=self.root/'empty.db';empty.touch()
        unrelated=self.root/'unrelated.db'
        source=sqlite3.connect(unrelated)
        try:source.execute('CREATE TABLE other(id INTEGER)');source.commit()
        finally:source.close()
        for source_path in (empty,unrelated):
            original=hashlib.sha256(source_path.read_bytes()).hexdigest()
            for apply in (False,True):
                with self.subTest(path=source_path.name,apply=apply):
                    with self.assertRaisesRegex(ValueError,'unsupported legacy database'):
                        import_legacy(self.store,source_path,'owner',apply=apply)
                    self.assertEqual(self.store.stats('owner')['messages'],0)
                    self.assertEqual(self.store.job_stats()['total'],0)
                    self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(),original)

    def test_deleting_original_cleans_linked_import_note_and_reimport_does_not_resurrect(self):
        import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        original=self.store.find_message('owner','chat-A','legacy:fixture:raw:1')
        note=self.store.find_message('owner','chat-A','legacy:fixture:memory:10','assistant')
        self.assertIsNotNone(note)
        self.store.delete_message('owner',original['id'])
        self.assertIsNone(self.store.get_message('owner',note['id']))
        self.assertFalse(self.store.lexical_search('owner','钥匙和提醒的历史摘要','chat-A',kind='derived'))
        again=import_legacy(self.store,self.legacy,'owner',apply=True,origin_key='fixture')
        self.assertTrue(again['ok']);self.assertEqual(again['suppressed_memories'],1)
        self.assertIsNone(self.store.find_message('owner','chat-A','legacy:fixture:memory:10','assistant'))


if __name__=='__main__':unittest.main()
