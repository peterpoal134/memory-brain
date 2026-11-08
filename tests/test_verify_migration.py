from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest

from scripts.verify_migration import verify_migration
from xiaojiu_brain.migration import import_legacy
from xiaojiu_brain.storage import Store


class VerifyMigrationTests(unittest.TestCase):
    def test_reconciliation_is_read_only_and_detects_body_mismatch_without_printing_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'old.db';destination=Path(tmp)/'new.db'
            old=sqlite3.connect(source)
            old.executescript('''
                CREATE TABLE raw_messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,sender TEXT,text TEXT,created_at REAL,turn_id TEXT);
                CREATE TABLE memories(id INTEGER PRIMARY KEY,kind TEXT,text TEXT,importance REAL,created_at REAL,visibility TEXT,source TEXT,source_msg_id INTEGER,meta TEXT);
            ''')
            old.execute('INSERT INTO raw_messages VALUES(?,?,?,?,?,?,?)',(1,'session-test','user','person','合成的原话内容',123,'stable-1'))
            old.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)',(1,'episode','合成历史笔记',.25,123,'public','digest',1,json.dumps({'msg_ids':[1]})))
            old.commit();old.close()
            store=Store(destination)
            try:import_legacy(store,source,'test-owner',apply=True,origin_key='fixture')
            finally:store.close()
            before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,destination)}
            result=verify_migration(source,destination,'test-owner','fixture',['session-test'])
            self.assertTrue(result['ok'],result['problems'])
            self.assertEqual(result['counts']['verified']['raw'],1)
            self.assertEqual(result['counts']['verified']['historical_notes'],1)
            self.assertEqual(result['warnings'],[])
            self.assertNotIn('合成的原话内容',json.dumps(result,ensure_ascii=False))
            self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,destination)})
            changed=sqlite3.connect(destination)
            try:
                changed.execute("UPDATE messages SET text='损坏的不同内容' WHERE message_id='legacy:fixture:raw:1'")
                changed.commit()
            finally:changed.close()
            failed=verify_migration(source,destination,'test-owner','fixture')
            self.assertFalse(failed['ok'])
            self.assertIn('raw_text_mismatch',{p['code'] for p in failed['problems']})
            self.assertNotIn('损坏的不同内容',json.dumps(failed,ensure_ascii=False))


if __name__=='__main__':unittest.main()
