"""Real owner-scoped memory nodes, without recall, models or production data."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config


class MemoryMapHTTPTests(unittest.TestCase):
    NOW = 1_800_000_000.
    DAY = 86400
    OWNER = "map-owner"
    TOKEN = "synthetic-memory-map-management-0123456789"
    RESTRICTED = "受限记忆，请在控制面板明确查看"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=self.tmp.name, owner=self.OWNER, api_token=self.TOKEN,
                             embed_url="http://127.0.0.1:1/embeddings",
                             llm_url="http://127.0.0.1:1/v1", llm_model="isolated-map-stub")
        self.app = BrainApp(self.config, start_worker=False)
        self.llm_patch = patch.object(self.app.llm, "complete_json", side_effect=AssertionError("unexpected model call"))
        self.embed_patch = patch.object(self.app.embedder, "embed_many", side_effect=AssertionError("unexpected embedding call"))
        self.llm_call = self.llm_patch.start()
        self.embed_call = self.embed_patch.start()
        self.server = BrainServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.app.close()
        self.llm_patch.stop()
        self.embed_patch.stop()
        self.tmp.cleanup()

    def request(self, path, *, auth=True, headers=None, now=None):
        given = {"Authorization": "Bearer " + self.TOKEN} if auth else {}
        given.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            with patch("xiaojiu_brain.memory_map.time.time", return_value=self.NOW if now is None else now):
                connection.request("GET", path, headers=given)
                response = connection.getresponse()
                body = json.loads(response.read())
                return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def map(self, *, now=None, **params):
        code, _, body = self.request("/v1/memory-map" + (("?" + urlencode(params)) if params else ""), now=now)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        return body["data"]

    def detail(self, ident):
        return self.request("/v1/memory-map/" + quote(ident, safe=""))

    def raw(self, key, *, text=None, when=None, owner=None, conversation="chat", metadata=None, created=None):
        owner = owner or self.OWNER
        text = text or "合成来源" + str(key)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW - 60 if created is None else created):
            ack = self.app.store.ingest(owner, conversation, str(key), "user", text,
                recorded_at=self.NOW - self.DAY if when is None else when, metadata=metadata)
        return {"id": ack["id"], "text": text, "owner": owner, "conversation": conversation,
                "document_ids": ack["document_ids"]}

    @staticmethod
    def ref(raw, start=0, end=None):
        return {"message_id": raw["id"], "start": start,
                "end": len(raw["text"]) if end is None else end}

    def derived(self, key, *, kind="fact", text=None, when=None, owner=None,
                source=None, refs=None, dependency_ids=None, disclosure="normal", metadata=None, created=None):
        source = source or self.raw(key, when=when, owner=owner)
        with patch("xiaojiu_brain.storage.time.time", return_value=self.NOW - 60 if created is None else created):
            return self.app.store.add_derived(source["owner"], source["conversation"], kind,
                text or "派生索引" + str(key), refs or [self.ref(source)], metadata=metadata,
                disclosure=disclosure, dependency_message_ids=dependency_ids)

    def test_default_uses_real_fact_summary_active_owner_recorded_window(self):
        fact = self.derived("recent-fact")
        summary = self.derived("recent-summary", kind="summary")
        self.raw("raw-not-in-default")
        self.derived("old", when=self.NOW - 31 * self.DAY)
        self.derived("future", when=self.NOW + 1)
        self.derived("foreign", owner="foreign-owner")
        archived = self.derived("archived")
        self.app.store.update_memory(self.OWNER, archived["id"], {"status": "archived"})
        data = self.map()
        self.assertEqual({item["id"] for item in data["items"]}, {fact["id"], summary["id"]})
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["days"], "30")
        self.assertEqual(data["kind"], "memories")
        self.assertEqual(data["timestamp_basis"], "recorded_at")
        self.assertEqual(data["until"], self.NOW)

    def test_explicit_day_and_kind_filters_do_not_rewrite_the_time_basis(self):
        recent = self.derived("two-days", when=self.NOW - 2 * self.DAY)
        middle = self.derived("twenty-days", kind="summary", when=self.NOW - 20 * self.DAY)
        older = self.derived("sixty-days", when=self.NOW - 60 * self.DAY)
        ancient = self.derived("hundred-days", when=self.NOW - 100 * self.DAY)
        self.assertEqual({x["id"] for x in self.map(days="7")["items"]}, {recent["id"]})
        self.assertEqual({x["id"] for x in self.map(days="30")["items"]}, {recent["id"], middle["id"]})
        self.assertEqual({x["id"] for x in self.map(days="90")["items"]}, {recent["id"], middle["id"], older["id"]})
        self.assertEqual({x["id"] for x in self.map(days="all")["items"]},
                         {recent["id"], middle["id"], older["id"], ancient["id"]})
        self.assertEqual({x["kind"] for x in self.map(kind="raw")["items"]}, {"raw"})
        self.assertEqual({x["id"] for x in self.map(kind="summary")["items"]}, {middle["id"]})
        self.assertTrue(all(x["kind"] == "fact" for x in self.map(kind="fact")["items"]))
        self.assertEqual(self.map(kind="all", days="all")["total"], 8)

    def test_paging_until_freezes_window_and_excludes_later_backfilled_documents(self):
        originals = [self.derived("page-" + str(i), when=self.NOW - (i + 1) * 100) for i in range(5)]
        baseline = self.map(limit=300)
        first = self.map(limit=2)
        frozen = first["until"]
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 2)
        self.derived("backfilled-after-snapshot", when=self.NOW - 50, created=self.NOW + 10)
        second = self.map(limit=2, offset=first["next_offset"], until=frozen, now=self.NOW + 20)
        third = self.map(limit=2, offset=second["next_offset"], until=frozen, now=self.NOW + 20)
        combined = first["items"] + second["items"] + third["items"]
        self.assertEqual([x["id"] for x in combined], [x["id"] for x in baseline["items"]])
        self.assertEqual({x["id"] for x in combined}, {x["id"] for x in originals})
        self.assertEqual((second["total"], third["total"]), (5, 5))
        self.assertFalse(third["has_more"])
        self.assertIsNone(third["next_offset"])

    def test_auth_origin_and_invalid_filters_cannot_bypass_scope_or_future_cutoff(self):
        self.assertEqual(self.request("/v1/memory-map", auth=False)[0], 401)
        for headers in ({"Origin": "https://foreign.invalid"}, {"Host": "foreign.invalid"}):
            self.assertEqual(self.request("/v1/memory-map", headers=headers)[0], 403)
        for params in [
            {"days": "31"}, {"days": "30.0"}, {"kind": "unknown"},
            {"limit": 0}, {"limit": 301}, {"limit": "1.5"},
            {"offset": -1}, {"offset": 100001}, {"offset": "0.5"},
            {"until": "nan"}, {"until": "inf"}, {"until": self.NOW + 60},
        ]:
            with self.subTest(params=params):
                self.assertEqual(self.request("/v1/memory-map?" + urlencode(params))[0], 400)
        self.derived("future-half-second", when=self.NOW + .25)
        near_future = self.map(until=self.NOW + .5)
        self.assertEqual(near_future["until"], self.NOW)
        self.assertEqual(near_future["items"], [])

    def test_restricted_sources_and_uncited_context_cannot_be_weakened_by_stale_projection(self):
        private = self.raw("private", text="PRIVATE_SOURCE_BODY", metadata={
            "disclosure": "only_if_asked", "disclosure_topics": ["PRIVATE_TOPIC"], "tag": "PRIVATE_TAG"})
        derived = self.derived("private-index", source=private, text="PRIVATE_DERIVED_BODY")
        public = self.raw("public-citation", text="公开逐字依据")
        hidden_context = self.raw("hidden-context", text="PRIVATE_CONTEXT_BODY", metadata={"disclosure": "never_say"})
        contextual = self.derived("context-index", source=public, text="PRIVATE_CONTEXT_INDEX",
                                 dependency_ids=[public["id"], hidden_context["id"]])
        # A legacy projection may carry stale normal metadata. Sources remain authoritative.
        self.app.store.conn.execute("UPDATE documents SET disclosure='normal' WHERE id IN (?,?)", (derived["id"], contextual["id"]))
        data = self.map(kind="all")
        by_id = {x["id"]: x for x in data["items"]}
        for ident in [derived["id"], contextual["id"], *private["document_ids"], *hidden_context["document_ids"]]:
            self.assertTrue(by_id[ident]["restricted"])
            self.assertEqual(by_id[ident]["preview"], self.RESTRICTED)
        serialized = json.dumps(data, ensure_ascii=False)
        for marker in ("PRIVATE_SOURCE_BODY", "PRIVATE_TOPIC", "PRIVATE_TAG", "PRIVATE_DERIVED_BODY",
                       "PRIVATE_CONTEXT_BODY", "PRIVATE_CONTEXT_INDEX"):
            self.assertNotIn(marker, serialized)
        self.assertTrue(all("metadata" not in item for item in data["items"]))

    def test_archived_restricted_overlap_protects_raw_but_not_disjoint_public_span(self):
        raw = self.raw("split-source", text="公开段落|PRIVATE_OVERLAP_BODY")
        public = self.derived("public-span", source=raw, refs=[self.ref(raw, 0, 4)], text="公开索引")
        private = self.derived("private-span", source=raw, refs=[self.ref(raw, 5, len(raw["text"]))],
                               text="PRIVATE_OVERLAP_INDEX", disclosure="never_say")
        self.app.store.update_memory(self.OWNER, private["id"], {"status": "archived"})
        data = self.map(kind="all")
        by_id = {x["id"]: x for x in data["items"]}
        self.assertNotIn(private["id"], by_id)
        self.assertTrue(by_id[raw["document_ids"][0]]["restricted"])
        self.assertEqual(by_id[raw["document_ids"][0]]["preview"], self.RESTRICTED)
        self.assertFalse(by_id[public["id"]]["restricted"])
        self.assertEqual(by_id[public["id"]]["preview"], "公开索引")

    def test_source_ids_are_bounded_unique_and_ignore_foreign_or_other_conversation_rows(self):
        sources = [self.raw("source-" + str(i)) for i in range(10)]
        doc = self.derived("many-sources", source=sources[0], refs=[self.ref(s) for s in sources], text="十个同范围来源")
        foreign = self.raw("foreign-source", owner="different-owner")
        other_chat = self.raw("other-chat-source", conversation="different-chat")
        for source in (foreign, other_chat):
            self.app.store.conn.execute("INSERT INTO document_sources VALUES(?,?,?,?)",
                                        (doc["id"], source["id"], 0, len(source["text"])))
        item = next(x for x in self.map()["items"] if x["id"] == doc["id"])
        self.assertEqual(item["source_count"], 10)
        self.assertEqual(len(item["source_ids"]), 8)
        self.assertEqual(len(set(item["source_ids"])), 8)
        self.assertTrue(set(item["source_ids"]) <= {s["id"] for s in sources})

    def test_preview_is_bounded_and_repeated_reads_are_model_free_and_read_only(self):
        document = self.derived("long-preview", text="长文本索引" * 100)
        counts = {table: self.app.store.conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                  for table in ("messages", "documents", "jobs", "model_attempts", "life_events")}
        changes = self.app.store.conn.total_changes
        for _ in range(3):
            item = next(x for x in self.map()["items"] if x["id"] == document["id"])
            self.assertLessEqual(len(item["preview"]), 220)
            self.assertTrue(item["preview"].startswith("长文本索引"))
        self.assertEqual(self.app.store.conn.total_changes, changes)
        self.assertEqual({table: self.app.store.conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                          for table in counts}, counts)
        self.llm_call.assert_not_called()
        self.embed_call.assert_not_called()

    def test_explicit_detail_keeps_source_restrictions_and_foreign_ids_hidden(self):
        normal = self.derived("detail-normal", text="完整记忆正文" * 100)
        secret = self.raw("detail-secret-source", text="PRIVATE_DETAIL_BODY", metadata={"disclosure": "never_say"})
        restricted = self.derived("detail-restricted", source=secret, text="PRIVATE_DETAIL_INDEX")
        self.app.store.conn.execute("UPDATE documents SET disclosure='normal' WHERE id=?", (restricted["id"],))
        foreign = self.derived("detail-foreign", owner="foreign-owner", text="PRIVATE_FOREIGN_DETAIL")
        changes = self.app.store.conn.total_changes
        self.assertEqual(self.request("/v1/memory-map/" + quote(normal["id"], safe=""), auth=False)[0], 401)
        code, _, body = self.detail(normal["id"])
        self.assertEqual(code, 200, body)
        self.assertEqual(body["data"]["full_text"], normal["text"])
        code, _, body = self.detail(restricted["id"])
        self.assertEqual(code, 200, body)
        self.assertTrue(body["data"]["restricted"])
        self.assertEqual(body["data"]["preview"], self.RESTRICTED)
        self.assertIsNone(body["data"]["full_text"])
        self.assertEqual(body["data"]["sources"], [])
        self.assertNotIn("PRIVATE_DETAIL", json.dumps(body))
        self.assertEqual(self.detail(foreign["id"])[0], 404)
        self.assertEqual(self.detail("unknown-memory-id")[0], 404)
        self.assertEqual(self.app.store.conn.total_changes, changes)
        self.llm_call.assert_not_called()
        self.embed_call.assert_not_called()

    def test_imported_note_is_not_original_speaker_or_import_time_as_event_time(self):
        old_time = self.NOW - 100 * self.DAY
        imported = self.raw("legacy-note", text="旧笔记原样保存", when=old_time,
                            metadata={"source_kind": "imported_memory", "original_role": "unknown"},
                            created=self.NOW - 10)
        self.assertEqual(self.map(kind="raw", days="30")["items"], [])
        data = self.map(kind="raw", days="all")
        item = next(x for x in data["items"] if x["id"] in imported["document_ids"])
        self.assertEqual(item["source_kind"], "imported_note")
        self.assertIsNone(item["role"])
        self.assertEqual(item["recorded_at"], old_time)
        self.assertNotEqual(item["recorded_at"], self.NOW - 10)

    def test_detail_sources_never_expand_public_spans_into_neighboring_private_text(self):
        first, second = "公开🪐片段甲", "另一个公开片段乙"
        private = "PRIVATE_NEIGHBOR_MUST_NOT_APPEAR"
        text = "UNQUOTED_PREFIX|" + first + "|" + private + "|" + second + "|UNQUOTED_SUFFIX"
        raw = self.raw("mixed-disclosure-source", text=text)
        spans = [(text.index(value), text.index(value) + len(value)) for value in (first, second)]
        public = self.derived("two-public-spans", source=raw, text="两个公开片段的索引",
                              refs=[self.ref(raw, start, end) for start, end in spans])
        secret_start = text.index(private)
        restricted = self.derived("protected-neighbor", source=raw, text="PRIVATE_INDEX_TEXT",
            refs=[self.ref(raw, secret_start, secret_start + len(private))], disclosure="never_say")
        changes = self.app.store.conn.total_changes
        code, _, body = self.detail(public["id"])
        self.assertEqual(code, 200, body)
        item = body["data"]
        self.assertFalse(item["restricted"])
        self.assertEqual(item["source_ids"], [raw["id"]])
        self.assertEqual(item["source_count"], 1)
        sources = item["sources"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sorted((s["start"], s["end"]) for s in sources), sorted(spans))
        for source in sources:
            self.assertEqual(source["message_id"], raw["id"])
            self.assertEqual(source["text"], text[source["start"]:source["end"]])
            self.assertIn(source["text"], (first, second))
            self.assertEqual(source["role"], "user")
            self.assertEqual(source["source_kind"], "original")
        serialized = json.dumps(body, ensure_ascii=False)
        for unquoted in (private, "UNQUOTED_PREFIX", "UNQUOTED_SUFFIX", "PRIVATE_INDEX_TEXT"):
            self.assertNotIn(unquoted, serialized)
        code, _, secret_body = self.detail(restricted["id"])
        self.assertEqual(code, 200, secret_body)
        self.assertEqual(secret_body["data"]["sources"], [])
        self.assertIsNone(secret_body["data"]["full_text"])
        self.assertEqual(secret_body["data"]["preview"], self.RESTRICTED)
        self.assertNotIn(private, json.dumps(secret_body))
        self.assertEqual(self.app.store.conn.total_changes, changes)
        self.llm_call.assert_not_called()
        self.embed_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
