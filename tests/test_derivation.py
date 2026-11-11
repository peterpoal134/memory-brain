import json
from pathlib import Path
import tempfile
import threading
import unittest

from xiaojiu_brain.storage import Store,JobLeaseLost
from xiaojiu_brain.derivation import DerivationWorker


class EmbedStub:
    model='isolated-vector-stub-v1'
    timeout=1
    def __init__(self,fail=False):self.calls=0;self.fail=fail;self.seen=[]
    def embed_many(self,texts):
        self.calls+=1;self.seen.extend(texts)
        if self.fail:raise RuntimeError('simulated provider outage')
        return [[1.,.5] for _ in texts]


class LLMStub:
    model='isolated-json-stub-v1'
    timeout=1
    def __init__(self,callback=None):self.calls=0;self.inputs=[];self.callback=callback
    def complete_json(self,system,user,max_tokens=1200):
        self.calls+=1;packet=json.loads(user);self.inputs.append(packet)
        if self.callback:return self.callback(packet)
        return {'documents':[{'kind':'fact','text':m['text'],'source_refs':[{'message_id':m['message_id'],'quote':m['text']}],
              'metadata':{'subject':'recorded_subject','attribute':'recorded_statement'}} for m in packet['messages']]}


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'brain.db';self.s=Store(self.path)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def ingest(self,text='钥匙在蓝盒子',owner='o',conv='c',mid='m1',**kwargs):return self.s.ingest(owner,conv,mid,'user',text,**kwargs)
    def ready(self):self.s.conn.execute("UPDATE jobs SET available_at=0 WHERE status='retry'")

    def test_missing_models_are_visible_and_raw_is_searchable(self):
        self.ingest();r=DerivationWorker(self.s).run_once()
        self.assertEqual(r['waiting_provider'],['embedding','derivation'])
        self.assertEqual(self.s.job_stats()['waiting_provider'],2)
        self.assertTrue(self.s.lexical_search('o','钥匙','c'))
        self.assertEqual(self.s.list_memories('o'),[])

    def test_embedding_failure_survives_close_reopen_and_recovers(self):
        self.ingest();bad=EmbedStub(fail=True)
        DerivationWorker(self.s,embedder=bad).run_once()
        self.assertEqual(self.s.job_stats()['retry'],1)
        self.assertEqual(self.s.stats('o')['vectors'],0)
        self.s.close();self.s=Store(self.path);self.ready()
        good=EmbedStub();DerivationWorker(self.s,embedder=good).run_once()
        self.assertEqual(self.s.stats('o')['vectors'],1)
        self.assertEqual(self.s.job_stats()['done'],1)
        self.assertEqual(len(self.s.list_messages('o')),1)

    def test_source_quotes_are_validated_and_conflicting_values_preserved(self):
        self.ingest('灯原来是蓝色');self.ingest('灯现在改成红色',mid='m2')
        llm=LLMStub();worker=DerivationWorker(self.s,llm=llm)
        r=worker.run_once();self.assertEqual(r['derived'],2)
        memories=self.s.list_memories('o')
        self.assertEqual({d['text'] for d in memories},{'灯原来是蓝色','灯现在改成红色'})
        self.assertTrue(all(d['metadata']['verification']=='source_spans_validated_not_semantic_proof' for d in memories))
        self.assertEqual(worker.run_once()['derived'],0)
        self.assertEqual(llm.calls,1)

    def test_fabricated_source_quote_is_failure_not_fabricated_fact(self):
        self.ingest()
        def bad(p):return {'documents':[{'kind':'fact','text':'钥匙在红盒子','source_refs':[{'message_id':p['messages'][0]['message_id'],'quote':'钥匙在红盒子'}]}]}
        worker=DerivationWorker(self.s,llm=LLMStub(bad));r=worker.run_once()
        self.assertTrue(r['errors']);self.assertEqual(self.s.list_memories('o'),[])
        self.assertTrue(self.s.lexical_search('o','蓝盒子','c'))
        self.assertEqual(self.s.job_stats()['retry'],1)

    def test_batch_validation_is_atomic(self):
        self.ingest()
        def bad(p):
            m=p['messages'][0]
            return {'documents':[{'kind':'fact','text':'合法提议','source_refs':[{'message_id':m['message_id'],'quote':m['text']}]},
                 {'kind':'fact','text':'无来源提议','source_refs':[{'message_id':9999,'start':0,'end':2}]}]}
        DerivationWorker(self.s,llm=LLMStub(bad)).run_once()
        self.assertEqual(self.s.stats('o')['memories'],0)
        self.assertEqual(self.s.job_stats()['done'],0)

    def test_failed_empty_response_can_retry_without_reingesting(self):
        self.ingest();worker=DerivationWorker(self.s,llm=LLMStub(lambda p:{}))
        worker.run_once();self.assertEqual(self.s.job_stats()['retry'],1)
        worker.llm=LLMStub();self.ready();worker.run_once()
        self.assertEqual(self.s.stats('o')['memories'],1)
        self.assertEqual(self.s.stats('o')['messages'],1)

    def test_empty_documents_is_explicit_success_without_invented_summary(self):
        self.ingest('今天只是打个招呼')
        worker=DerivationWorker(self.s,llm=LLMStub(lambda p:{'documents':[]}))
        worker.run_once();self.assertEqual(self.s.job_stats()['done'],1)
        self.assertEqual(self.s.list_memories('o'),[])
        self.assertTrue(self.s.lexical_search('o','打个招呼','c'))

    def test_lease_takeover_fences_stale_worker_before_any_write(self):
        original=self.ingest();old=self.s.claim_jobs('derive',worker_id='old')
        self.s.conn.execute("UPDATE jobs SET lease_until=0 WHERE worker_id='old'")
        fresh=self.s.claim_jobs('derive',worker_id='fresh')
        docs=[{'kind':'fact','text':'钥匙在蓝盒子','source_refs':[{'message_id':original['id'],'start':0,'end':len('钥匙在蓝盒子')}]}]
        with self.assertRaises(JobLeaseLost):self.s.complete_derivation(old,'old',docs)
        self.s.fail_jobs(old,'old','stale error')
        self.assertEqual(self.s.job_stats()['running'],1)
        self.s.complete_derivation(fresh,'fresh',docs)
        self.assertEqual(len(self.s.list_memories('o')),1)
        self.assertEqual(self.s.job_stats()['done'],1)

    def test_never_say_does_not_enter_model_batches(self):
        self.ingest('私人暗号不能说',metadata={'disclosure':'never_say'})
        self.ingest('午饭吃了面条',mid='normal')
        embed,llm=EmbedStub(),LLMStub();worker=DerivationWorker(self.s,embed,llm)
        worker.run_once()
        self.assertNotIn('私人暗号不能说',embed.seen)
        self.assertTrue(all(m['text']!='私人暗号不能说' for p in llm.inputs for m in p['messages']))
        self.assertEqual(self.s.job_stats()['blocked_policy'],2)

    def test_derivation_batches_do_not_mix_owners_conversations_or_disclosures(self):
        self.ingest('公开记录')
        self.ingest('健康记录',mid='private',metadata={'disclosure':'only_if_asked','disclosure_topics':['健康']})
        self.ingest('工资记录',mid='salary',metadata={'disclosure':'only_if_asked','disclosure_topics':['工资']})
        self.ingest('另一位的记录',owner='other')
        self.ingest('另一个会话',conv='other')
        llm=LLMStub();worker=DerivationWorker(self.s,llm=llm)
        for _ in range(5):worker.run_once()
        self.assertEqual(len(llm.inputs),5)
        self.assertTrue(all(len(p['messages'])==1 for p in llm.inputs))
        for d in self.s.list_memories('o'):
            if d['text']=='健康记录':self.assertEqual(d['disclosure'],'only_if_asked')

    def test_deletion_during_provider_call_cannot_resurrect_evidence(self):
        original=self.ingest()
        def delete(p):
            m=p['messages'][0];self.s.delete_message('o',m['message_id'])
            return {'documents':[{'kind':'fact','text':m['text'],'source_refs':[{'message_id':m['message_id'],'quote':m['text']}]}]}
        DerivationWorker(self.s,llm=LLMStub(delete)).run_once()
        self.assertEqual(self.s.documents('o'),[])
        self.assertIsNone(self.s.get_message('o',original['id']))

    def test_delete_any_generation_context_cleans_potentially_tainted_derivation(self):
        a=self.ingest('一号记录');b=self.ingest('二号记录',mid='m2')
        def one(p):
            m=p['messages'][0]
            return {'documents':[{'kind':'summary','text':'批次概述','source_refs':[{'message_id':m['message_id'],'quote':m['text']}]}]}
        DerivationWorker(self.s,llm=LLMStub(one)).run_once()
        self.assertEqual(len(self.s.list_memories('o')),1)
        # Although only message A was cited, B was sent to the model as context.
        self.s.delete_message('o',b['id'])
        self.assertEqual(self.s.list_memories('o'),[])
        self.assertIsNotNone(self.s.get_message('o',a['id']))

    def test_expired_job_recovered_by_new_worker_and_vector_count_guard(self):
        self.ingest();old=self.s.claim_jobs('embed',worker_id='crashed')
        self.s.conn.execute("UPDATE jobs SET lease_until=0 WHERE worker_id='crashed'")
        worker=DerivationWorker(self.s,embedder=EmbedStub());worker.run_once()
        self.assertEqual(self.s.stats('o')['vectors'],1)
        with self.assertRaises(JobLeaseLost):self.s.complete_vectors(old,'crashed',[[1.,1.]],'late-model')

    def test_start_stop_is_idempotent_and_can_use_no_models(self):
        self.ingest();worker=DerivationWorker(self.s,interval=.05)
        worker.start();thread=worker._thread;worker.start()
        self.assertIs(thread,worker._thread)
        self.assertTrue(worker.stop(timeout=1))
        self.assertTrue(worker.stop(timeout=1))


if __name__=='__main__':unittest.main()
