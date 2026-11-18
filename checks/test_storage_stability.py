"""Synthetic failure/concurrency regressions for Store; never opens live data."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from xiaojiu_brain.storage import Store


class AbortedOperation(BaseException):
    pass


class ConnectionFault:
    def __init__(self, connection, *, rollback_error=False, backup_error=False):
        self.connection=connection
        self.rollback_error=rollback_error
        self.backup_error=backup_error
    def __getattr__(self,name):
        return getattr(self.connection,name)
    def rollback(self):
        if self.rollback_error:raise sqlite3.OperationalError('synthetic rollback failure')
        return self.connection.rollback()
    def backup(self,destination):
        if self.backup_error:
            destination.execute('CREATE TABLE incomplete_snapshot(value TEXT)')
            raise sqlite3.OperationalError('synthetic backup failure')
        return self.connection.backup(destination)


class StorageStability(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='memory-brain-storage-test-')
        self.directory=Path(self.temp.name)
        self.store=Store(self.directory/'synthetic.db')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def ingest(self,ident='safe'):
        return self.store.ingest('owner','conversation',ident,'user','synthetic '+ident)

    def test_commit_failure_rolls_back_and_next_real_ingest_recovers(self):
        self.ingest('before')
        self.store.conn.executescript('''
            CREATE TABLE audit_parent(id INTEGER PRIMARY KEY);
            CREATE TABLE audit_child(parent_id INTEGER REFERENCES audit_parent(id) DEFERRABLE INITIALLY DEFERRED);
        ''')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction():
                self.store.conn.execute('INSERT INTO audit_child VALUES(99)')
        self.assertFalse(self.store.conn.in_transaction)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM audit_child').fetchone()[0],0)
        self.assertTrue(self.ingest('after')['accepted'])
        self.assertIsNotNone(self.store.find_message('owner','conversation','before'))
        self.assertEqual(self.store.conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_base_exception_rolls_back_without_committing_partial_originals(self):
        with self.assertRaises(AbortedOperation):
            with self.store._transaction():
                self.store.conn.execute("INSERT INTO messages(owner,conversation_id,message_id,role,text,recorded_at,metadata) VALUES('owner','c','aborted','user','synthetic',0,'{}')")
                raise AbortedOperation('synthetic cancellation')
        self.assertFalse(self.store.conn.in_transaction)
        self.assertIsNone(self.store.find_message('owner','c','aborted'))
        self.assertTrue(self.ingest()['accepted'])

    def test_rollback_failure_keeps_original_exception_primary_and_exposes_cause(self):
        connection=self.store.conn
        self.store.conn=ConnectionFault(connection,rollback_error=True)
        try:
            with self.assertRaises(AbortedOperation) as caught:
                with self.store._transaction():
                    raise AbortedOperation('original cancellation')
            self.assertEqual(str(caught.exception),'original cancellation')
            self.assertIsInstance(caught.exception.__cause__,sqlite3.OperationalError)
            self.assertIn('rollback failure',str(caught.exception.__cause__))
        finally:
            self.store.conn=connection
            connection.rollback()

    def test_backup_inside_write_transaction_rejects_before_creating_target(self):
        destination=self.directory/'must-not-publish.db'
        with self.store._transaction():
            self.store.conn.execute('CREATE TABLE not_committed(value TEXT)')
            with self.assertRaisesRegex(ValueError,'settled database transaction'):
                self.store.backup(destination)
            self.assertTrue(self.store.conn.in_transaction,'backup must not settle caller transaction')
            self.assertFalse(destination.exists())
        self.assertTrue(self.ingest()['accepted'])

    def test_failed_backup_removes_only_its_partial_target_and_can_retry(self):
        destination=self.directory/'retry.db'
        connection=self.store.conn
        self.store.conn=ConnectionFault(connection,backup_error=True)
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError,'synthetic backup failure'):
                self.store.backup(destination)
        finally:self.store.conn=connection
        self.assertFalse(destination.exists())
        self.ingest('retained')
        self.assertEqual(self.store.backup(destination)['integrity'],'ok')
        with closing(sqlite3.connect(destination)) as check:
            self.assertEqual(check.execute('SELECT count(*) FROM messages').fetchone()[0],1)

    def test_existing_backup_and_active_database_are_never_overwritten_or_removed(self):
        self.ingest('first')
        destination=self.directory/'preserved.db'
        self.store.backup(destination)
        digest=hashlib.sha256(destination.read_bytes()).hexdigest()
        self.ingest('second')
        with self.assertRaisesRegex(ValueError,'destination already exists'):
            self.store.backup(destination)
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(),digest)
        with self.assertRaisesRegex(ValueError,'active database'):
            self.store.backup(self.store.path)
        self.assertEqual(self.store.stats('owner')['messages'],2)

    def test_concurrent_same_destination_creates_one_snapshot_without_overwrite(self):
        self.ingest()
        destination=self.directory/'one-winner.db'
        barrier=threading.Barrier(2)
        def backup():
            barrier.wait()
            try:return self.store.backup(destination)['integrity']
            except ValueError as error:return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda _:backup(),range(2)))
        self.assertEqual(sorted(outcomes),['backup destination already exists','ok'])
        with closing(sqlite3.connect(destination)) as check:
            self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_existing_job_queue_reopens_with_index_and_preserves_policy_and_leases(self):
        self.store.conn.execute('DROP INDEX IF EXISTS ix_jobs_policy_lease')
        held=self.ingest('held')
        expired=self.ingest('expired')
        sealed=self.store.ingest('owner','conversation','sealed','user','synthetic sealed',
                                 metadata={'disclosure':'never_say'})
        imported=self.store.ingest('owner','conversation','imported','user','synthetic import',
                                   metadata={'import_model_policy':'local_only','skip_derivation':True})
        for worker in ('held-worker','expired-worker'):
            self.store.claim_derivation_jobs(limit=1,worker_id=worker,force=True)
        self.store.conn.execute("UPDATE jobs SET lease_until=0 WHERE worker_id='expired-worker'")
        before=self.store.conn.execute('SELECT * FROM jobs ORDER BY id').fetchall()
        schema_version=self.store.conn.execute('PRAGMA user_version').fetchone()[0]

        with closing(Store(self.store.path)) as reopened:
            self.assertEqual(reopened.conn.execute('SELECT * FROM jobs ORDER BY id').fetchall(),before)
            self.assertEqual(reopened.conn.execute('PRAGMA user_version').fetchone()[0],schema_version)
            index=next((row for row in reopened.conn.execute('PRAGMA index_list(jobs)')
                        if row['name']=='ix_jobs_policy_lease'),None)
            self.assertIsNotNone(index)
            self.assertEqual(index['partial'],1)

            executed=[]
            reopened.conn.set_trace_callback(executed.append)
            try:
                reopened.refresh_policy_jobs(import_embedding_is_remote=True)
                embed_claims=reopened.claim_jobs('embed',limit=10,worker_id='embed-worker')
                derive_claims=reopened.claim_derivation_jobs(limit=10,worker_id='derive-worker',force=True)
            finally:
                reopened.conn.set_trace_callback(None)
            self.assertEqual([job['document_id'] for job in embed_claims],
                             held['document_ids']+expired['document_ids'])
            self.assertEqual([job['message_id'] for job in derive_claims],[expired['id']])
            jobs=list(reopened.conn.execute('SELECT * FROM jobs'))
            sealed_jobs=[job for job in jobs if job['message_id']==sealed['id']
                         or job['document_id'] in sealed['document_ids']]
            self.assertEqual([job['status'] for job in sealed_jobs],['blocked_policy','blocked_policy'])
            hold=next(job for job in jobs if job['message_id']==held['id'])
            self.assertEqual((hold['status'],hold['worker_id']),('running','held-worker'))

            reopened.refresh_policy_jobs(import_embedding_is_remote=False)
            local_claims=reopened.claim_jobs('embed',limit=10,worker_id='local-worker')
            self.assertEqual([job['document_id'] for job in local_claims],imported['document_ids'])
            self.assertEqual(reopened.conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')

            updates=('blocked_policy','pending','retry')
            for status in updates:
                with self.subTest(status=status):
                    sql=next(statement for statement in executed
                             if statement.startswith(f"UPDATE jobs SET status='{status}'"))
                    plan=[row[3] for row in reopened.conn.execute('EXPLAIN QUERY PLAN '+sql)]
                    self.assertTrue(any('SEARCH jobs USING INDEX ix_jobs_policy_lease' in row for row in plan),plan)

    def test_small_concurrent_writes_reads_and_backup_keep_originals_and_extensions_consistent(self):
        self.store.conn.executescript("CREATE TABLE synthetic_extension(value TEXT);INSERT INTO synthetic_extension VALUES('retained');")
        barrier=threading.Barrier(4)
        destination=self.directory/'concurrent.db'
        def writer(number):
            barrier.wait()
            for index in range(12):
                self.store.ingest('owner','conversation',f'{number}-{index}','user','synthetic concurrent source')
        def reader():
            barrier.wait()
            for _ in range(12):
                docs=self.store.documents('owner')
                for doc in docs:self.assertEqual(doc['owner'],'owner')
        def backup():
            barrier.wait()
            return self.store.backup(destination)
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending=[pool.submit(writer,0),pool.submit(writer,1),pool.submit(reader),pool.submit(backup)]
            for future in pending:future.result(timeout=5)
        self.assertEqual(self.store.stats('owner')['messages'],24)
        self.assertEqual(self.store.conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        with closing(sqlite3.connect(destination)) as check:
            self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(check.execute('SELECT value FROM synthetic_extension').fetchone()[0],'retained')
            count=check.execute('SELECT count(*) FROM messages').fetchone()[0]
            self.assertEqual(check.execute('SELECT count(*) FROM ingest_keys').fetchone()[0],count)
            self.assertEqual(check.execute('SELECT count(*) FROM documents').fetchone()[0],count)
            self.assertEqual(check.execute('PRAGMA foreign_key_check').fetchall(),[])


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(StorageStability)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    summary={'run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'production_mutations':0,'model_calls':0}
    print(json.dumps(summary))
    raise SystemExit(0 if result.wasSuccessful() else 1)
