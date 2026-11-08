"""HTTP acceptance of the isolated app; never starts model workers or old services."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config
from xiaojiu_brain.policy import estimate_tokens
from xiaojiu_brain.providers import EmbeddingProvider, ProviderError


class HTTPTests(unittest.TestCase):
    token = "local-test-token-0123456789"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=self.directory.name, owner="owner-a", api_token=self.token)
        self.start()

    def start(self):
        self.app = BrainApp(self.config, start_worker=False)
        self.server = BrainServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()

    def tearDown(self):
        self.stop()
        self.directory.cleanup()

    def request(self, method, path, data=None, authorized=True, headers=None, raw=None):
        given = {"Authorization": "Bearer " + self.token} if authorized else {}
        if headers:
            given.update(headers)
        body = raw
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf8")
            given["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=given)
            response = conn.getresponse()
            payload = response.read()
            ctype = response.getheader("Content-Type", "")
            result = json.loads(payload) if ctype.startswith("application/json") else payload
            return response.status, dict(response.getheaders()), result
        finally:
            conn.close()

    def ok(self, method, path, data=None):
        code, _, result = self.request(method, path, data)
        self.assertEqual(code, 200, result)
        self.assertTrue(result["ok"], result)
        return result["data"]

    def ingest(self, mid, text, **extra):
        return self.ok("POST", "/v1/ingest", {"conversation_id": "conversation-a", "message_id": mid,
            "role": "user", "text": text, **extra})

    def recall(self, query, **extra):
        return self.ok("POST", "/v1/recall", {"conversation_id": "conversation-a", "query": query, **extra})

    def test_status_auth_and_credential_free_public_health(self):
        code, headers, body = self.request("GET", "/health", authorized=False)
        self.assertEqual(code, 200)
        self.assertTrue(body["data"]["auth_required"])
        self.assertEqual(self.request("GET", "/v1/status", authorized=False)[0], 401)
        code, headers, body = self.request("GET", "/v1/status")
        self.assertEqual(code, 200)
        self.assertNotIn(self.token, json.dumps(body))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_origin_host_and_absolute_external_url_are_rejected(self):
        for headers in [{"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"},
                        {"Origin": "null"}, {"Origin": f"https://127.0.0.1:{self.port}"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/v1/status", headers=headers)[0], 403)
        code, _, _ = self.request("GET", "http://evil.invalid/v1/status")
        self.assertIn(code, (400, 403))

    def test_real_http_ingest_deduplication_and_current_question_exclusion(self):
        record = self.ingest("old", "项目的旧名称叫蓝港。")
        self.assertTrue(record["accepted"])
        duplicate = self.ingest("old", "项目的旧名称叫蓝港。")
        self.assertTrue(duplicate["duplicate"])
        current = self.ingest("question", "项目的旧名称叫什么？")
        result = self.recall("项目的旧名称叫什么？", current_message_id="question")
        ids = {ref["message_id"] for e in result["evidence"] for ref in e["source_refs"]}
        self.assertIn(record["id"], ids)
        self.assertNotIn(current["id"], ids)
        self.assertIn("蓝港", result["block"])
        missing = self.recall("项目", current_message_id="not-archived")
        self.assertEqual(missing["block"], "")
        self.assertIn("current_message_not_archived", missing["degraded"])

    def test_current_internal_id_does_not_exclude_historical_external_id_with_same_digits(self):
        old = self.ingest("3", "项目的旧名称叫蓝港。")
        self.ingest("second", "收到，先继续。", role="assistant")
        question = self.ingest("question", "项目的旧名称叫什么？")
        self.assertEqual(question["id"], 3)
        result = self.recall("项目的旧名称叫什么？", current_message_id="question")
        ids = {ref["message_id"] for e in result["evidence"] for ref in e["source_refs"]}
        self.assertIn(old["id"], ids)
        self.assertNotIn(question["id"], ids)

    def test_owner_body_and_query_fields_cannot_impersonate_other_owner(self):
        self.ingest("spoof", "资料属于当前认证账号。", owner="owner-b")
        foreign = self.app.store.ingest("owner-b", "conversation-a", "private", "user", "FOREIGN-OWNER-SECRET")
        messages = self.ok("GET", "/v1/messages?owner=owner-b")["items"]
        self.assertEqual({m["owner"] for m in messages}, {"owner-a"})
        self.assertEqual(self.request("GET", f"/v1/evidence/{foreign['id']}")[0], 404)
        result = self.recall("FOREIGN-OWNER-SECRET", owner="owner-b")
        self.assertEqual(result["evidence"], [])

    def test_unknown_routes_bad_payloads_and_missing_static_files(self):
        self.assertEqual(self.request("GET", "/v1/does-not-exist")[0], 404)
        for payload in [[], {"role": "system", "text": "injected", "message_id": "bad"},
                        {"role": "user", "text": "", "message_id": "empty"}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/v1/ingest", payload)[0], 400)
        self.assertEqual(self.request("POST", "/v1/recall", {"query": ""})[0], 400)
        self.assertEqual(self.request("POST", "/v1/recall", {"query": "资料", "token_budget": 1})[0], 400)
        self.assertEqual(self.request("GET", "/static/")[0], 404)
        for path in ["/static/%2e%2e/config.py", "/static/%2e%2e%2fconfig.py", "/static/%2e%2e%5cconfig.py"]:
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertIn(self.request("GET", "/v1/backups/")[0], (400, 404))

    def test_backup_download_is_a_valid_complete_sqlite_snapshot(self):
        self.ingest("before-backup", "备份原话必须完整保留。")
        backup = self.ok("POST", "/v1/backup", {})
        code, headers, archive = self.request("GET", backup["url"])
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("attachment", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(set(z.namelist()), {"brain.db", "preferences.json", "manifest.json"})
            target = Path(self.directory.name) / "downloaded-snapshot.db"
            target.write_bytes(z.read("brain.db"))
            self.assertNotIn(self.token, z.read("preferences.json").decode("utf8"))
        conn = sqlite3.connect(target)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT text FROM messages").fetchone()[0], "备份原话必须完整保留。")
        finally:
            conn.close()

    def test_http_semantic_mock_recalls_original_with_no_lexical_overlap_and_keeps_trace(self):
        original = self.ingest("bridge", "负责桥梁承载计算。")
        class SemanticMock:
            model = "http-test-model"
            def embed_many(self, texts):
                return [[1., 0.] for _ in texts]
        self.app.embedder = SemanticMock()
        for document in original["document_ids"]:
            self.app.store.set_vector("owner-a", document, [1., 0.], "http-test-model")
        result = self.recall("职业类别")
        self.assertIn("桥梁承载计算", result["block"])
        self.assertEqual(result["trace"]["routes"]["raw_lexical"], 0)
        self.assertEqual(result["trace"]["routes"]["raw_semantic"], 1)
        trace = self.ok("GET", "/v1/trace/" + result["trace_id"])
        self.assertEqual(trace["owner"], "owner-a")
        self.app._traces[result["trace_id"]]["owner"] = "owner-b"
        self.assertEqual(self.request("GET", "/v1/trace/" + result["trace_id"])[0], 404)

    def test_only_if_asked_requires_explicit_public_topic_or_owner_selected_topic(self):
        self.ok("POST", "/v1/remember", {"conversation_id": "conversation-a", "text": "门禁密码是DEMO-7284。",
            "disclosure": "only_if_asked", "disclosure_topics": ["门禁密码"]})
        for query in ["如何进门", "不要说门禁密码，聊晚饭", "DEMO-7284"]:
            with self.subTest(query=query):
                result = self.recall(query)
                self.assertEqual(result["block"], "")
                self.assertTrue(any(g["code"] == "disclosure_withheld" for g in result["coverage_gaps"]))
        self.assertIn("DEMO-7284", self.recall("门禁密码")["block"])
        self.assertIn("DEMO-7284", self.recall("进门资料", requested_topics=["门禁密码"])["block"])
        self.assertEqual(self.request("POST", "/v1/recall", {"query": "资料", "requested_topics": "门禁密码"})[0], 400)

    def test_never_say_is_not_injected_even_with_explicit_topic(self):
        self.ok("POST", "/v1/remember", {"conversation_id": "conversation-a", "text": "保险柜密码是NEVER-82。",
            "disclosure": "never_say", "disclosure_topics": ["保险柜密码"]})
        result = self.recall("保险柜密码", requested_topics=["保险柜密码"])
        self.assertEqual(result["block"], "")
        self.assertNotIn("NEVER-82", json.dumps(result, ensure_ascii=False))

    def test_handwritten_memory_immediate_recall_pin_archive_and_delete_provenance(self):
        note = self.ok("POST", "/v1/remember", {"conversation_id": "conversation-a", "text": "取件凭证放在蓝色文件夹。",
            "importance": .9, "pinned": True})
        result = self.recall("取件凭证")
        self.assertIn("蓝色文件夹", result["block"])
        source = self.ok("GET", f"/v1/evidence/{note['id']}")
        self.assertEqual(source["text"], "取件凭证放在蓝色文件夹。")
        changed = self.ok("PATCH", "/v1/memories/" + note["document_id"], {"pinned": False, "status": "archived"})
        self.assertEqual(changed["status"], "archived")
        self.assertFalse(changed["pinned"])
        archived = self.ok("GET", "/v1/memories?status=archived")["items"]
        self.assertIn(note["document_id"], [row["id"] for row in archived])
        self.ok("DELETE", "/v1/memories/" + note["document_id"], {})
        self.assertEqual(self.ok("GET", f"/v1/evidence/{note['id']}")["text"], source["text"])
        self.ok("DELETE", f"/v1/messages/{note['id']}", {})
        self.assertEqual(self.request("GET", f"/v1/evidence/{note['id']}")[0], 404)
        self.assertEqual(self.recall("取件凭证")["evidence"], [])

    def test_export_has_originals_but_never_management_or_model_credentials(self):
        self.ingest("export", "导出的正文应是用户原话。")
        code, headers, exported = self.request("GET", "/v1/export")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        serialized = json.dumps(exported, ensure_ascii=False)
        self.assertIn("导出的正文应是用户原话。", serialized)
        self.assertNotIn(self.token, serialized)
        self.assertNotIn("api_token", serialized)
        self.assertNotIn("llm_key_env", serialized)
        for field in ["llm_url", "llm_key_env", "api_token", "embed_url"]:
            self.assertEqual(self.request("PATCH", "/v1/settings", {field: "not-allowed"})[0], 400)

    def test_self_emotion_and_settings_are_persistent_and_non_factual_by_default(self):
        self.ok("POST", "/v1/self", {"text": "角色希望先给简短结论。", "style_preferences": "简洁中文"})
        emotion = self.ok("PATCH", "/v1/emotion", {"pleasure": .7, "arousal": .2, "tags": ["高兴"], "notes": "由用户设置"})
        self.assertTrue(emotion["signal_only"])
        self.assertAlmostEqual(emotion["pleasure"], .7, delta=.01)
        self.assertNotIn("角色希望", self.recall("资料")["block"])
        self.ok("PATCH", "/v1/settings", {"token_budget": 900, "self_notes_enabled": True, "emotion_enabled": True})
        self.assertIn("不能作为客观事实", self.recall("资料")["block"])
        self.stop()
        self.start()
        settings = self.ok("GET", "/v1/settings")
        self.assertEqual(settings["token_budget"], 900)
        self.assertTrue(settings["self_notes_enabled"])
        self.assertEqual(self.ok("GET", "/v1/self")["notes"][0]["text"], "角色希望先给简短结论。")
        self.assertIn("高兴", self.ok("GET", "/v1/emotion")["tags"])

    def test_capture_and_recall_toggles_do_not_delete_existing_records(self):
        original = self.ingest("kept", "已有记录是湖畔见面。")
        self.ok("PATCH", "/v1/settings", {"capture_enabled": False, "recall_enabled": False})
        paused = self.ingest("blocked", "不应保存这句。")
        self.assertFalse(paused["accepted"])
        self.assertEqual(paused["reason"], "capture_paused")
        self.assertEqual(self.recall("已有记录")["block"], "")
        self.ok("PATCH", "/v1/settings", {"capture_enabled": True, "recall_enabled": True})
        self.assertIn("湖畔见面", self.recall("已有记录")["block"])
        self.assertEqual(len(self.ok("GET", "/v1/messages")["items"]), 1)
        self.assertEqual(self.ok("GET", f"/v1/evidence/{original['id']}")["text"], "已有记录是湖畔见面。")

    def test_invalid_remember_request_must_not_partially_ingest_raw_or_jobs(self):
        before = self.ok("GET", "/v1/status")["stats"]
        for importance in ["not-a-number", 1.5, -1]:
            code, _, _ = self.request("POST", "/v1/remember", {"conversation_id": "conversation-a", "text": "不应由无效请求保存。", "importance": importance})
            self.assertEqual(code, 400)
        after = self.ok("GET", "/v1/status")["stats"]
        self.assertEqual(after, before)

    def test_invalid_self_patch_is_atomic_and_does_not_leave_partial_note(self):
        before = self.ok("GET", "/v1/self")
        self.assertEqual(self.request("POST", "/v1/self", {"text": "失败请求不应留下的笔记。", "notes": [{}]})[0], 400)
        self.assertEqual(self.ok("GET", "/v1/self"), before)

    def test_personalization_cannot_silently_exceed_the_declared_request_budget(self):
        self.ok("POST", "/v1/self", {"text": "角色描述。" * 200})
        self.ok("PATCH", "/v1/settings", {"self_notes_enabled": True})
        result = self.recall("无记录问题", token_budget=128)
        self.assertLessEqual(estimate_tokens(result["block"]), 128)

    def test_malformed_origin_fails_as_response_not_crashed_connection(self):
        self.assertIn(self.request("GET", "/v1/status", headers={"Origin": "http://["})[0], (400, 403))

    def test_null_byte_static_path_fails_as_response_not_crashed_connection(self):
        self.assertIn(self.request("GET", "/static/%00config.py")[0], (400, 404))

    def test_explicit_cross_conversation_switch_changes_scope_but_not_owner(self):
        self.ok("POST", "/v1/ingest", {"conversation_id": "other-conversation", "message_id": "other-conv", "role": "user", "text": "跨会话资料是黄桥。"})
        self.assertEqual(self.recall("跨会话资料")["evidence"], [])
        self.ok("PATCH", "/v1/settings", {"cross_conversation": True})
        self.assertIn("黄桥", self.recall("跨会话资料")["block"])


class ProviderHTTPTests(unittest.TestCase):
    """The upstream here is a local HTTP protocol stub, never a model."""
    def setUp(self):
        self.response_body = {"data": []}
        self.inputs = []
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.inputs.append(data["input"])
                body = json.dumps(test.response_body).encode("utf8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.provider = EmbeddingProvider("synthetic-test", f"http://127.0.0.1:{self.server.server_port}/embeddings", timeout=2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_out_of_order_embeddings_are_mapped_by_explicit_index(self):
        self.response_body = {"data": [{"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}]}
        self.assertEqual(self.provider.embed_many(["first", "second"]), [[1., 0.], [0., 1.]])
        self.assertEqual(self.inputs, [["first", "second"]])

    def test_duplicate_or_out_of_range_indexes_are_rejected_instead_of_misindexing_sources(self):
        for indexes in [(0, 0), (0, 2), (-1, 1)]:
            with self.subTest(indexes=indexes):
                self.response_body = {"data": [{"index": i, "embedding": [1., 0.]} for i in indexes]}
                with self.assertRaises(ProviderError):
                    self.provider.embed_many(["first", "second"])

    def test_dimension_changes_are_rejected_before_vector_storage(self):
        self.response_body = {"data": [{"index": 0, "embedding": [1., 0.]}, {"index": 1, "embedding": [1.]}]}
        with self.assertRaises(ProviderError):
            self.provider.embed_many(["first", "second"])


if __name__ == "__main__":
    unittest.main()
