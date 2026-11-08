import json
from pathlib import Path
import tempfile
import unittest

from xiaojiu_brain.policy import estimate_tokens
from xiaojiu_brain.retrieval import Retriever
from xiaojiu_brain.storage import Store


class FixedEmbedder:
    model = "test-embedding-v1"

    def embed_many(self, texts):
        return [[1., 0.] for _ in texts]


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "brain.db")
        self.next_id = 0

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def add(self, text, role="user", owner="u", conversation="c", stamp=100, metadata=None):
        self.next_id += 1
        return self.store.ingest(owner, conversation, str(self.next_id), role, text,
                                 recorded_at=stamp, metadata=metadata)

    def vector(self, record, vector=None, model="test-embedding-v1", owner="u"):
        for doc in record["document_ids"]:
            self.store.set_vector(owner, doc, vector or [1., 0.], model)

    def recall(self, query, **kwargs):
        return Retriever(self.store).recall("u", "c", query, **kwargs)

    def test_semantic_candidate_with_no_lexical_match_and_zero_importance(self):
        record = self.add("负责桥梁承载计算。", metadata={"importance": 0.})
        self.vector(record)
        self.assertEqual(self.store.lexical_search("u", "职业类别", conversation_id="c"), [])
        result = Retriever(self.store, FixedEmbedder()).recall("u", "c", "职业类别")
        self.assertIn("桥梁承载计算", result["block"])
        self.assertEqual(result["trace"]["routes"]["raw_lexical"], 0)
        self.assertEqual(result["trace"]["routes"]["raw_semantic"], 1)

    def test_owner_conversation_and_time_are_scoped_before_ranking(self):
        wanted = self.add("灯塔项目：先核对图纸。", stamp=100)
        self.add("灯塔项目：未来变更。", stamp=200)
        self.add("灯塔项目：另一会话。", conversation="other", stamp=100)
        self.add("灯塔项目：另一个用户。", owner="other", stamp=100)
        result = self.recall("灯塔项目", as_of=150)
        self.assertEqual({r["message_id"] for e in result["evidence"] for r in e["source_refs"]}, {wanted["id"]})
        self.assertEqual(result["trace"]["scoped_documents"], 1)

    def test_cross_conversation_requires_explicit_administrative_configuration(self):
        with self.assertRaises(ValueError):
            Retriever(self.store).recall("u", None, "资料")

    def test_first_person_name_survives_assistant_address_and_third_party_clauses(self):
        record = self.add("我叫程澈，你叫我名字就行；同事说她叫林霁。")
        result = self.recall("我的名字")
        self.assertEqual(result["evidence"][0]["source_refs"][0]["message_id"], record["id"])
        self.assertIn("我叫程澈", result["block"])

    def test_factual_statement_ending_in_a_question_is_not_deleted(self):
        original = "我的生日是10月8日，你能记住吗？"
        self.add(original)
        result = self.recall("我的生日")
        self.assertEqual(result["evidence"][0]["text"], original)

    def test_relative_duration_not_in_a_keyword_dictionary_stays_retrievable(self):
        self.add("申请72小时后交，电子版一并提交。")
        self.assertIn("72小时后", self.recall("申请何时交")["block"])

    def test_multi_fact_evidence_preserves_both_sources_and_unlisted_location(self):
        gift = self.add("送给季禾的礼物是一套陶瓷杯。")
        place = self.add("季禾的礼物暂存在餐厅壁橱第三格。", stamp=200)
        result = self.recall("季禾的礼物是什么，暂时放在哪儿？")
        ids = {r["message_id"] for e in result["evidence"] for r in e["source_refs"]}
        self.assertEqual(ids, {gift["id"], place["id"]})
        self.assertIn("陶瓷杯", result["block"])
        self.assertIn("壁橱第三格", result["block"])

    def test_same_entity_correction_does_not_erase_other_property(self):
        advice = self.add("教练以前建议我保留旧训练日志。")
        self.add("更正一下，我的教练叫岑衡。", stamp=200)
        result = self.recall("教练以前建议我保留什么？")
        self.assertIn(advice["id"], {r["message_id"] for e in result["evidence"] for r in e["source_refs"]})

    def test_two_positive_conflicting_values_and_structured_claims_are_preserved(self):
        one = self.add("母亲一直住在南宁。")
        two = self.add("母亲一直住在柳州。", stamp=200)
        for record, value in [(one, "南宁"), (two, "柳州")]:
            text = self.store.get_message("u", record["id"])["text"]
            self.store.add_derived("u", "c", "fact", "母亲常住" + value,
                [{"message_id": record["id"], "start": 0, "end": len(text)}],
                metadata={"claim": {"subject": "mother", "predicate": "residence", "value": value}})
        result = self.recall("母亲常住地有两种说法，能确定吗？")
        self.assertIn("南宁", result["block"])
        self.assertIn("柳州", result["block"])
        self.assertEqual(result["conflicts"][0]["values"], ["南宁", "柳州"])
        self.assertFalse(result["conflicts"][0]["claims_verified"])

    def test_assistant_own_progress_keeps_role_and_source(self):
        self.add("已经提交三件工单，还剩两件没回执。", role="assistant")
        result = self.recall("你提交的工单还剩几件？")
        self.assertIn("还剩两件", result["block"])
        self.assertEqual(result["evidence"][0]["role"], "assistant")
        self.assertIn("助手", result["block"])

    def test_derived_wording_cannot_replace_original_quote(self):
        record = self.add("鞋子收在柜子里面。")
        self.store.add_derived("u", "c", "summary", "鞋子放在柜子后面。",
            [{"message_id": record["id"], "start": 0, "end": len("鞋子收在柜子里面。") }])
        result = self.recall("鞋子柜子后面")
        self.assertIn("鞋子收在柜子里面。", result["block"])
        self.assertNotIn("鞋子放在柜子后面。", result["block"])
        self.assertTrue(result["assertions"])
        self.assertFalse(result["assertions"][0]["claims_verified"])

    def test_never_say_is_filtered_before_reranking_and_cannot_be_requested(self):
        private = self.add("门禁密码是PRIV-7284。", metadata={"disclosure": "never_say", "disclosure_topics": ["门禁密码"]})
        self.vector(private)
        public = self.add("门禁开放时间是上午九点。")
        self.vector(public)
        class RecordingReranker:
            def score(inner, query, documents):
                self.assertNotIn("PRIV-7284", " ".join(documents))
                return [1.] * len(documents)
        result = Retriever(self.store, FixedEmbedder(), RecordingReranker()).recall("u", "c", "门禁密码", requested_topics=["门禁密码"])
        self.assertNotIn("PRIV-7284", json.dumps(result, ensure_ascii=False))
        self.assertTrue(any(g["code"] == "disclosure_withheld" for g in result["coverage_gaps"]))

    def test_only_if_asked_is_not_unlocked_by_related_query_or_secret_value(self):
        private = self.add("门禁密码是PRIV-7284。", metadata={"disclosure": "only_if_asked", "disclosure_topics": ["门禁密码"]})
        self.vector(private)
        retriever = Retriever(self.store, FixedEmbedder())
        for query in ["最近如何进门", "PRIV-7284", "不要提门禁密码，聊晚饭"]:
            with self.subTest(query=query):
                result = retriever.recall("u", "c", query)
                self.assertEqual(result["evidence"], [])
                self.assertNotIn("PRIV-7284", result["block"])
        self.assertIn("PRIV-7284", retriever.recall("u", "c", "门禁密码")["block"])

    def test_sensitive_source_blocks_another_derived_projection(self):
        original = self.add("项目代号是SENSITIVE-42。")
        refs = [{"message_id": original["id"], "start": 0, "end": len("项目代号是SENSITIVE-42。") }]
        self.store.add_derived("u", "c", "fact", "项目代号敏感", refs, disclosure="never_say")
        result = self.recall("项目代号")
        self.assertNotIn("SENSITIVE-42", result["block"])
        self.assertEqual(result["evidence"], [])

    def test_budget_omission_is_reported_without_clipping_the_source(self):
        self.add("档案说明：" + "完整条件与例外。" * 40)
        result = self.recall("档案说明", token_budget=0)
        self.assertEqual(result["block"], "")
        self.assertEqual(result["evidence"], [])
        self.assertTrue(any(g["code"] == "no_evidence_fits_budget" for g in result["coverage_gaps"]))

    def test_provider_failures_fall_back_without_leaking_error_contents(self):
        self.add("申请已经提交。")
        class BrokenEmbedding:
            model = "broken"
            def embed_many(self, texts):
                raise RuntimeError("DO-NOT-LEAK-CREDENTIAL")
        class BrokenReranker:
            def score(self, query, documents):
                raise ValueError("DO-NOT-LEAK-CREDENTIAL")
        result = Retriever(self.store, BrokenEmbedding(), BrokenReranker()).recall("u", "c", "申请")
        self.assertIn("申请已经提交", result["block"])
        self.assertIn("embedding_provider_error:RuntimeError", result["degraded"])
        self.assertIn("reranker_provider_error:ValueError", result["degraded"])
        self.assertNotIn("DO-NOT-LEAK-CREDENTIAL", json.dumps(result))

    def test_model_version_mismatch_is_visible_and_never_cosine_scored(self):
        record = self.add("申请已经提交。")
        self.vector(record, model="different-model")
        result = Retriever(self.store, FixedEmbedder()).recall("u", "c", "申请")
        self.assertEqual(result["trace"]["routes"]["raw_semantic"], 0)
        self.assertIn("semantic_index_incomplete", result["degraded"])

    def test_source_roles_times_and_exact_spans_are_roundtrippable(self):
        text = "独立资料：这是原话，不是摘要。"
        record = self.add(text, role="assistant")
        result = self.recall("独立资料")
        for evidence in result["evidence"]:
            for ref in evidence["source_refs"]:
                source = self.store.get_message("u", ref["message_id"])
                self.assertEqual(evidence["text"], source["text"][ref["start"]:ref["end"]])
                self.assertEqual(evidence["recorded_at"], source["recorded_at"])
                self.assertEqual(ref["role"], "assistant")
        self.assertLessEqual(estimate_tokens(result["block"]), 1200)
        self.assertEqual(result["evidence"][0]["source_refs"][0]["message_id"], record["id"])

    def test_exclude_internal_or_external_message_id(self):
        record = self.add("申请当前问题本身。")
        self.assertEqual(self.recall("申请", exclude_message_ids=[record["id"]])["evidence"], [])
        self.assertEqual(self.recall("申请", exclude_message_ids=["1"])["evidence"], [])

    def test_absence_of_candidates_does_not_claim_history_never_contained_fact(self):
        result = self.recall("未知物品")
        self.assertTrue(any(g["code"] == "no_ranked_candidates" for g in result["coverage_gaps"]))
        self.assertFalse(result["trace"]["coverage_certified"])

    def test_archived_lexical_document_is_only_available_when_explicitly_requested(self):
        record = self.add("归档项目的依据是纸质验收单。")
        self.store.update_memory("u", record["document_ids"][0], {"status": "archived"})
        self.assertEqual(self.recall("归档项目")["evidence"], [])
        self.assertIn("纸质验收单", self.recall("归档项目", include_archived=True)["block"])

    def test_raw_lexical_route_cannot_be_starved_by_more_than_a_thousand_summaries(self):
        text = "共同关键词：" + "背景材料。" * 100
        original = self.add(text)
        ref = [{"message_id": original["id"], "start": 0, "end": len(text)}]
        for i in range(1001):
            self.store.add_derived("u", "c", "summary", "共同关键词" + str(i), ref)
        result = self.recall("共同关键词")
        self.assertGreater(result["trace"]["routes"]["raw_lexical"], 0)
        self.assertEqual(result["trace"]["routes"]["derived_lexical"], 40)

    def test_reranker_reads_validated_originals_instead_of_unverified_derived_words(self):
        original = self.add("鞋子收在柜子里面。")
        self.store.add_derived("u", "c", "summary", "鞋子放在柜子后面。",
            [{"message_id": original["id"], "start": 0, "end": len("鞋子收在柜子里面。") }])
        class SourceOnlyReranker:
            def score(inner, query, documents):
                self.assertNotIn("柜子后面", " ".join(documents))
                self.assertIn("柜子里面", " ".join(documents))
                return [1.] * len(documents)
        result = Retriever(self.store, reranker=SourceOnlyReranker()).recall("u", "c", "鞋子")
        self.assertNotIn("reranker_provider_error:AssertionError", result["degraded"])

    def test_recorded_iso_offset_is_preserved_for_relative_time_anchor(self):
        self.next_id += 1
        self.store.ingest("u", "c", str(self.next_id), "user", "昨天已经提交项目申请。",
            recorded_at="2026-06-03T00:15:00+08:00")
        result = self.recall("项目申请")
        self.assertIn("2026-06-03T00:15:00+08:00", result["block"])

    def test_invalid_source_from_foreign_owner_is_rejected_even_if_backend_returns_document(self):
        private = self.add("项目代号是OTHER-OWNER-PRIVATE。", owner="other")
        class BadBackend:
            def documents(inner, owner, **kwargs):
                return [{"id": "forged", "kind": "fact", "owner": "u", "conversation_id": "c",
                         "text": "项目代号", "recorded_at": 100, "status": "active",
                         "source_refs": [{"message_id": private["id"], "start": 0, "end": 5}],
                         "metadata": {}, "disclosure": "normal"}]
            def lexical_search(inner, owner, query, **kwargs):
                return inner.documents(owner)
            def get_message(inner, owner, mid):
                return self.store.get_message(owner, mid)
        result = Retriever(BadBackend()).recall("u", "c", "项目代号")
        self.assertEqual(result["evidence"], [])
        self.assertGreater(result["trace"]["invalid_source_documents"], 0)

    def test_imported_legacy_note_is_not_presented_as_assistant_speech(self):
        self.add("旧记录中住址写着南岸。", role="assistant",
                 metadata={"source_kind": "imported_memory", "original_role": "unknown", "original_kind": "fact"})
        result = self.recall("住址")
        self.assertTrue(result["evidence"])
        self.assertEqual(result["evidence"][0]["role"], "unknown")
        self.assertFalse(result["evidence"][0]["is_original_utterance"])
        self.assertIn("旧库导入笔记（非对话原话", result["block"])
        self.assertIn("原说话者未核实", result["block"])
        self.assertNotIn("原话#", result["block"])
        self.assertEqual(result["evidence"][0]["source_refs"][0]["source_time_semantics"],
                         "legacy_record_timestamp_not_verified_utterance_time")

    def test_owner_authored_manual_note_is_distinguished_from_dialogue(self):
        self.add("以后笔记都加日期。", metadata={"source_kind": "owner_note"})
        result = self.recall("笔记")
        self.assertIn("用户手写笔记", result["block"])
        self.assertFalse(result["evidence"][0]["is_original_utterance"])

    def test_internal_exclusion_id_does_not_collide_with_another_external_numeric_id(self):
        historical = self.store.ingest("u", "c", "3", "user", "项目旧名叫蓝港。", recorded_at=100)
        self.store.ingest("u", "c", "second", "assistant", "收到。", recorded_at=101)
        current = self.store.ingest("u", "c", "question", "user", "项目旧名叫什么？", recorded_at=102)
        self.assertEqual(current["id"], 3)
        result = self.recall("项目旧名叫什么？", exclude_message_ids=[current["id"]])
        ids = {ref["message_id"] for e in result["evidence"] for ref in e["source_refs"]}
        self.assertIn(historical["id"], ids)
        self.assertNotIn(current["id"], ids)


if __name__ == "__main__":
    unittest.main()
