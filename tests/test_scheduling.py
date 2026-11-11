"""Scheduling contracts: bounded paid work, durable splits and independent vectors.

All records and providers are synthetic. Events, a frozen clock and durable job
rows establish the conditions; these tests never wait fifteen real minutes.
"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from xiaojiu_brain.derivation import DerivationWorker
from xiaojiu_brain.providers import OutputTruncatedError
from xiaojiu_brain.storage import Store


class VectorRecorder:
    model = "scheduling-local-vector-stub"
    is_local = True
    timeout = .1

    def __init__(self, marker=None, observed=None):
        self.seen = []
        self.marker = marker
        self.observed = observed

    def embed_many(self, texts):
        self.seen.extend(texts)
        if self.marker and any(self.marker in text for text in texts):
            self.observed.set()
        return [[1., .5] for _ in texts]


class ProposalRecorder:
    model = "scheduling-json-stub"
    timeout = .5
    supports_request_context = False

    def __init__(self, action=None):
        self.inputs = []
        self.action = action

    @staticmethod
    def proposal(packet):
        return {"documents": [
            {"kind": "fact", "text": item["text"], "source_refs": [
                {"message_id": item["message_id"], "quote": item["text"]}
            ]}
            for item in packet["messages"]
        ]}

    def complete_json(self, system, user, max_tokens=1200):
        packet = json.loads(user)
        self.inputs.append(packet)
        if self.action:
            return self.action(packet)
        return self.proposal(packet)


class SchedulingTests(unittest.TestCase):
    NOW = 1_800_000_000.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "brain.db"
        self.s = Store(self.path)
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.stop(timeout=2)
        self.s.close()
        self.tmp.cleanup()

    def worker(self, **kwargs):
        instance = DerivationWorker(self.s, **kwargs)
        self.workers.append(instance)
        return instance

    def ingest(self, number, *, owner="owner", conversation="chat", text=None,
               metadata=None, recorded_at=None):
        accepted = self.s.ingest(owner, conversation, "message-" + str(number), "user",
                                 text or "合成记录" + str(number),
                                 metadata=metadata, recorded_at=recorded_at)
        return self.s.get_message(owner, accepted["id"])

    def add_messages(self, count, **kwargs):
        return [self.ingest(i, **kwargs) for i in range(count)]

    def derive_jobs(self):
        return [dict(row) for row in self.s.conn.execute(
            "SELECT * FROM jobs WHERE kind='derive' ORDER BY id")]

    def source_ids(self):
        return {ref["message_id"] for doc in self.s.list_memories("owner")
                for ref in doc["source_refs"]}

    def make_retries_available(self):
        self.s.conn.execute("UPDATE jobs SET available_at=0 WHERE status='retry'")

    def test_repeated_polling_of_fresh_small_group_does_not_call_llm(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            self.add_messages(3)
            for _ in range(5):
                worker.run_derivation_once()
        self.assertEqual(llm.inputs, [])
        self.assertTrue(all(j["status"] == "pending" for j in self.derive_jobs()))
        self.assertEqual(len(self.s.list_messages("owner")), 3)

    def test_default_full_batch_is_twelve_messages_not_twelve_turns(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            messages = self.add_messages(13)
            worker.run_derivation_once()
            worker.run_derivation_once()
        self.assertEqual([len(p["messages"]) for p in llm.inputs], [12])
        self.assertEqual(self.source_ids(), {m["id"] for m in messages[:12]})
        self.assertEqual([j["status"] for j in self.derive_jobs()], ["done"] * 12 + ["pending"])

    def test_sparse_group_runs_at_wait_boundary_using_enqueue_age(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            self.ingest(1)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW + 899):
            worker.run_derivation_once()
        self.assertEqual(llm.inputs, [])
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW + 900):
            worker.run_derivation_once()
        self.assertEqual(len(llm.inputs), 1)
        self.assertEqual(self.derive_jobs()[0]["status"], "done")

    def test_historical_recorded_time_cannot_bypass_fresh_enqueue_wait(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            self.ingest(1, recorded_at=self.NOW - 365 * 86400)
            worker.run_derivation_once()
        self.assertEqual(llm.inputs, [])
        self.assertEqual(self.derive_jobs()[0]["status"], "pending")

    def test_explicit_manual_run_once_preserves_force_drain_compatibility(self):
        llm, embedder = ProposalRecorder(), VectorRecorder()
        worker = self.worker(llm=llm, embedder=embedder)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            original = self.ingest(1)
            worker.run_once()
        self.assertEqual(len(llm.inputs), 1)
        self.assertEqual(self.source_ids(), {original["id"]})
        self.assertIn(original["text"], embedder.seen)

    def test_conversations_and_owners_cannot_combine_to_meet_threshold(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm, min_batch_messages=3)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            self.add_messages(2, conversation="one")
            self.add_messages(2, conversation="two")
            self.add_messages(2, owner="another", conversation="one")
            worker.run_derivation_once()
            self.assertEqual(llm.inputs, [])
            final = self.ingest("third", conversation="two")
            worker.run_derivation_once()
        expected = {m["id"] for m in self.s.list_messages("owner", "two")}
        self.assertIn(final["id"], expected)
        self.assertEqual({m["message_id"] for m in llm.inputs[0]["messages"]}, expected)
        self.assertEqual(len(llm.inputs), 1)

    def test_disclosure_topics_are_separate_readiness_groups(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm, min_batch_messages=3)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            for i in range(2):
                self.ingest("normal" + str(i))
                self.ingest("a" + str(i), metadata={"disclosure": "only_if_asked", "disclosure_topics": ["topic-a"]})
                self.ingest("b" + str(i), metadata={"disclosure": "only_if_asked", "disclosure_topics": ["topic-b"]})
            worker.run_derivation_once()
            self.assertEqual(llm.inputs, [])
            self.ingest("a2", metadata={"disclosure": "only_if_asked", "disclosure_topics": ["topic-a"]})
            worker.run_derivation_once()
        sent = llm.inputs[0]["messages"]
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(m["text"].startswith("合成记录a") for m in sent))

    def test_small_old_group_does_not_hide_another_ready_group(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm, min_batch_messages=3)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            old = self.ingest("old", conversation="not-full")
            ready = self.add_messages(3, conversation="full")
            worker.run_derivation_once()
        self.assertEqual(self.source_ids(), {m["id"] for m in ready})
        self.assertEqual(next(j for j in self.derive_jobs() if j["message_id"] == old["id"])["status"], "pending")

    def test_ready_retry_does_not_wait_for_eleven_new_messages(self):
        count = [0]
        def intermittent(packet):
            count[0] += 1
            if count[0] == 1:
                raise RuntimeError("synthetic outage")
            return ProposalRecorder.proposal(packet)
        llm = ProposalRecorder(intermittent)
        worker = self.worker(llm=llm)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW):
            self.ingest(1)
            worker.run_derivation_once(force=True)
            self.make_retries_available()
            worker.run_derivation_once()
        self.assertEqual(len(llm.inputs), 2)
        self.assertEqual(self.derive_jobs()[0]["status"], "done")

    def test_llm_block_does_not_hold_up_live_raw_embedding_thread(self):
        entered, release, embedded = threading.Event(), threading.Event(), threading.Event()
        def blocking(packet):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test barrier was not released")
            return ProposalRecorder.proposal(packet)
        llm = ProposalRecorder(blocking)
        vectors = VectorRecorder("fresh-vector-marker", embedded)
        worker = self.worker(llm=llm, embedder=vectors, interval=.05,
                             embedding_interval=.05, min_batch_messages=1)
        self.ingest("slow-derive")
        worker.start()
        try:
            self.assertTrue(entered.wait(1), "background derivation never reached the barrier")
            self.ingest("fresh", text="fresh-vector-marker", metadata={"skip_derivation": True})
            self.assertTrue(embedded.wait(1), "embedding waited for the blocked LLM")
            self.assertFalse(release.is_set())
        finally:
            release.set()
            self.assertTrue(worker.stop(timeout=2))

    def test_turning_off_derivation_keeps_raw_vectors_available(self):
        vectors = VectorRecorder()
        worker = self.worker(embedder=vectors, llm=None)
        self.ingest(1)
        worker.run_derivation_once()
        worker.run_embedding_once()
        self.assertEqual(self.s.stats("owner")["vectors"], 1)
        self.assertEqual(self.s.stats("owner")["messages"], 1)
        self.assertEqual(self.s.list_memories("owner"), [])

    def test_character_guard_splits_before_paid_call_without_cutting_originals(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm, max_input_chars=3000, min_batch_messages=4)
        originals = [self.ingest(i, text=str(i) + "甲" * 1000 + "尾") for i in range(4)]
        worker.run_derivation_once()
        self.assertEqual([len(p["messages"]) for p in llm.inputs], [2, 2])
        self.assertEqual(self.source_ids(), {m["id"] for m in originals})
        self.assertEqual({m["text"] for m in self.s.list_messages("owner")}, {m["text"] for m in originals})

    def test_output_truncation_splits_and_success_is_not_repeated(self):
        def limited(packet):
            if len(packet["messages"]) > 2:
                raise OutputTruncatedError(finish_reason="length")
            return ProposalRecorder.proposal(packet)
        llm = ProposalRecorder(limited)
        worker = self.worker(llm=llm, min_batch_messages=4)
        originals = self.add_messages(4)
        worker.run_derivation_once()
        worker.run_derivation_once(force=True)
        self.assertEqual([len(p["messages"]) for p in llm.inputs], [4, 2, 2])
        self.assertEqual(self.source_ids(), {m["id"] for m in originals})
        self.assertTrue(all(j["status"] == "done" for j in self.derive_jobs()))

    def test_successful_half_is_not_paid_again_after_failure_and_restart(self):
        def partial_failure(packet):
            mids = [m["message_id"] for m in packet["messages"]]
            if len(mids) == 4:
                raise OutputTruncatedError(finish_reason="length")
            if mids[0] == originals[2]["id"]:
                raise RuntimeError("synthetic failure of right half")
            return ProposalRecorder.proposal(packet)
        originals = self.add_messages(4)
        llm = ProposalRecorder(partial_failure)
        worker = self.worker(llm=llm, min_batch_messages=4, batch_size=4)
        worker.run_derivation_once()
        self.assertEqual(self.source_ids(), {m["id"] for m in originals[:2]})
        self.s.close()
        self.s = Store(self.path)
        self.make_retries_available()
        self.ingest("new-1")
        self.ingest("new-2")
        resumed = ProposalRecorder()
        recovered = self.worker(llm=resumed, min_batch_messages=4, batch_size=4)
        recovered.run_derivation_once()
        self.assertEqual(len(resumed.inputs), 1)
        self.assertEqual({m["message_id"] for m in resumed.inputs[0]["messages"]},
                         {m["id"] for m in originals[2:]})
        self.assertEqual(len(resumed.inputs[0]["messages"]), 2,
                         "restarted worker merged an already split batch back to its old size")
        self.assertEqual(self.source_ids(), {m["id"] for m in originals})

    def test_single_truncated_message_is_terminal_without_repeated_paid_attempts(self):
        def always_truncated(packet):
            raise OutputTruncatedError(finish_reason="length")
        llm = ProposalRecorder(always_truncated)
        worker = self.worker(llm=llm, min_batch_messages=1)
        self.ingest(1)
        worker.run_derivation_once()
        worker.run_derivation_once(force=True)
        self.assertEqual(len(llm.inputs), 1)
        self.assertEqual(self.derive_jobs()[0]["status"], "failed")
        self.assertEqual(self.s.stats("owner")["messages"], 1)

    def test_unsplittable_huge_source_is_visible_without_silent_cutting(self):
        llm = ProposalRecorder()
        worker = self.worker(llm=llm, min_batch_messages=1)
        text = "原话开头" + "长" * 64001 + "原话尾部"
        original = self.ingest(1, text=text)
        worker.run_derivation_once()
        self.assertEqual(llm.inputs, [])
        self.assertEqual(self.s.get_message("owner", original["id"])["text"], text)
        self.assertEqual(self.derive_jobs()[0]["status"], "failed")

    def test_bad_source_in_a_split_half_is_still_atomic_and_not_split_to_accept(self):
        originals = self.add_messages(4)
        def bad_left(packet):
            if len(packet["messages"]) == 4:
                raise OutputTruncatedError(finish_reason="length")
            result = ProposalRecorder.proposal(packet)
            if packet["messages"][0]["message_id"] == originals[0]["id"]:
                result["documents"][1]["source_refs"][0]["quote"] = "不存在的逐字原话"
            return result
        llm = ProposalRecorder(bad_left)
        worker = self.worker(llm=llm, min_batch_messages=4)
        worker.run_derivation_once()
        self.assertEqual([len(p["messages"]) for p in llm.inputs], [4, 2, 2])
        self.assertEqual(self.source_ids(), {m["id"] for m in originals[2:]})
        statuses = {j["message_id"]: j["status"] for j in self.derive_jobs()}
        self.assertTrue(all(statuses[m["id"]] == "retry" for m in originals[:2]))
        self.assertTrue(all(statuses[m["id"]] == "done" for m in originals[2:]))

    def test_disabled_during_split_finishes_left_but_does_not_send_right(self):
        originals = self.add_messages(4)
        def disable_after_left(packet):
            if len(packet["messages"]) == 4:
                raise OutputTruncatedError(finish_reason="length")
            worker.llm = None
            return ProposalRecorder.proposal(packet)
        llm = ProposalRecorder(disable_after_left)
        worker = self.worker(llm=llm, min_batch_messages=4)
        worker.run_derivation_once()
        self.assertEqual([len(p["messages"]) for p in llm.inputs], [4, 2])
        self.assertEqual(self.source_ids(), {m["id"] for m in originals[:2]})
        self.assertEqual([j["status"] for j in self.derive_jobs()], ["done", "done", "pending", "pending"])
        resumed = ProposalRecorder()
        worker.llm = resumed
        worker.run_derivation_once(force=True)
        self.assertEqual([m["message_id"] for m in resumed.inputs[0]["messages"]],
                         [m["id"] for m in originals[2:]])

    def test_stop_during_split_releases_unattempted_right_half(self):
        self.add_messages(4)
        def stop_after_left(packet):
            if len(packet["messages"]) == 4:
                raise OutputTruncatedError(finish_reason="length")
            worker.stop(timeout=0)
            return ProposalRecorder.proposal(packet)
        llm = ProposalRecorder(stop_after_left)
        worker = self.worker(llm=llm, min_batch_messages=4)
        worker.run_derivation_once()
        self.assertEqual([len(p["messages"]) for p in llm.inputs], [4, 2])
        self.assertEqual([j["status"] for j in self.derive_jobs()], ["done", "done", "pending", "pending"])

    def test_terminal_old_failure_cannot_block_new_ready_group(self):
        old = self.ingest("old", conversation="old")
        self.s.conn.execute("UPDATE jobs SET status='failed',attempts=3 WHERE kind='derive' AND message_id=?", (old["id"],))
        ready = self.add_messages(3, conversation="new")
        llm = ProposalRecorder()
        worker = self.worker(llm=llm, min_batch_messages=3)
        worker.run_derivation_once()
        self.assertEqual(self.source_ids(), {m["id"] for m in ready})
        self.assertEqual(len(llm.inputs), 1)
        self.assertEqual(next(j for j in self.derive_jobs() if j["message_id"] == old["id"])["status"], "failed")

    def test_synthetic_provider_cannot_fabricate_paid_usage_ledger(self):
        self.add_messages(2)
        self.worker(llm=ProposalRecorder(), min_batch_messages=2).run_derivation_once()
        count = self.s.conn.execute("SELECT count(*) FROM model_attempts").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
