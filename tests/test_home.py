"""Real local HTTP contracts for the public shell and private observatory.

No production files or services, remote models, or browser are used. Browser
storage, navigation and WebGL behavior require their separate UI acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.config import Config


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in ("script", "img") and attrs.get("src"):
            self.assets.append((tag, attrs["src"]))
        if tag == "link" and "stylesheet" in attrs.get("rel", "").split() and attrs.get("href"):
            self.assets.append(("stylesheet", attrs["href"]))


class HomeHTTPTests(unittest.TestCase):
    OWNER = "private-owner-marker"
    TOKEN = "synthetic-home-token-01234567890123456789"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=self.tmp.name, owner=self.OWNER, api_token=self.TOKEN,
                             embed_url="http://127.0.0.1:1/embeddings",
                             llm_url="http://127.0.0.1:1/v1", llm_model="isolated-test-model")
        self.app = BrainApp(self.config, start_worker=False)
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
        self.tmp.cleanup()

    def request(self, method, path, body=None, *, auth=True, headers=None):
        request_headers = {"Authorization": "Bearer " + self.TOKEN} if auth else {}
        request_headers.update(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=payload, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            response_headers = dict(response.getheaders())
            if response.getheader("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, response_headers, data
        finally:
            connection.close()

    def ok(self, method, path, body=None):
        code, _, data = self.request(method, path, body)
        self.assertEqual(code, 200, data)
        self.assertIs(data["ok"], True)
        return data["data"]

    def observatory(self, suffix=""):
        return self.ok("GET", "/v1/observatory" + suffix)

    def seed_message(self, key, *, owner=None, text="合成原话"):
        return self.app.store.ingest(owner or self.OWNER, "chat", key, "user", text)

    def table_counts(self):
        tables = ("messages", "documents", "jobs", "model_attempts", "life_events", "life_books", "life_tasks")
        return {table: self.app.store.conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in tables}

    def seed_usage(self, *, ident="usage-1", owner=None, usage=None):
        event = {"attempt_id": ident, "started_at": 1_700_000_000., "elapsed_ms": 3.,
                 "model": "synthetic-accounting-fixture", "outcome": "success",
                 "error": None, "finish_reason": "stop", "usage": usage,
                 "request_context": {}}
        self.app.store.record_model_attempt(owner or self.OWNER, event)

    def test_root_and_brain_are_new_shell_while_legacy_panel_is_retained(self):
        root_code, root_headers, root = self.request("GET", "/", auth=False)
        brain_code, _, brain = self.request("GET", "/brain", auth=False)
        brain_slash_code, _, brain_slash = self.request("GET", "/brain/", auth=False)
        panel_code, panel_headers, panel = self.request("GET", "/panel", auth=False)
        slash_code, _, slash_panel = self.request("GET", "/panel/", auth=False)
        self.assertEqual((root_code, brain_code, brain_slash_code, panel_code, slash_code),
                         (200, 200, 200, 200, 200))
        self.assertIn("text/html", root_headers["Content-Type"])
        self.assertIn("text/html", panel_headers["Content-Type"])
        self.assertEqual(root, brain)
        self.assertEqual(root, brain_slash)
        self.assertEqual(panel, slash_panel)
        self.assertNotEqual(root, panel)
        self.assertIn(b'id="page-messages"', panel)
        self.assertNotIn(self.TOKEN.encode(), root + panel)
        homepage = root.decode("utf-8")
        self.assertIn("Memory Brain", homepage)
        self.assertIn("忆核大脑", homepage)
        self.assertNotIn("OrbitMemory", homepage)
        self.assertNotIn("星枢记忆", homepage)

    def test_shell_references_local_assets_with_real_mime_and_unchanged_csp(self):
        code, headers, body = self.request("GET", "/", auth=False)
        self.assertEqual(code, 200, body)
        policy = headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        parser = AssetReferences()
        parser.feed(body.decode("utf-8"))
        self.assertTrue(parser.assets, "brain shell has no inspectable local resources")
        for kind, reference in parser.assets:
            parsed = urlsplit(reference)
            with self.subTest(reference=reference):
                if kind == "img" and parsed.scheme == "data":
                    self.assertTrue(reference.startswith("data:image/"))
                    continue
                self.assertFalse(parsed.scheme or parsed.netloc, "external assets violate the offline local shell")
                resolved = urlsplit(urljoin("http://local/", reference))
                target = resolved.path + (("?" + resolved.query) if resolved.query else "")
                code, asset_headers, asset = self.request("GET", target, auth=False)
                self.assertEqual(code, 200)
                self.assertTrue(asset)
                if kind == "script":
                    self.assertIn("javascript", asset_headers["Content-Type"])
                elif kind == "stylesheet":
                    self.assertIn("text/css", asset_headers["Content-Type"])

    def test_public_shell_health_does_not_make_observatory_public(self):
        self.assertEqual(self.request("GET", "/", auth=False)[0], 200)
        code, _, health = self.request("GET", "/health", auth=False)
        self.assertEqual(code, 200)
        self.assertTrue(health["data"]["auth_required"])
        self.assertEqual(self.request("GET", "/v1/observatory", auth=False)[0], 401)
        self.assertNotIn("storage", health["data"])
        self.assertNotIn(self.OWNER, json.dumps(health))

    def test_new_routes_reject_foreign_origin_or_host_without_cors(self):
        for path in ("/", "/brain", "/v1/observatory"):
            for header in ({"Origin": "https://foreign.invalid"}, {"Origin": "null"}, {"Host": "foreign.invalid"}):
                with self.subTest(path=path, header=header):
                    code, headers, _ = self.request("GET", path, headers=header)
                    self.assertEqual(code, 403)
                    self.assertNotIn("Access-Control-Allow-Origin", headers)
        code, _, _ = self.request("GET", "http://foreign.invalid/v1/observatory")
        self.assertIn(code, (400, 403))

    def test_bootstrap_one_use_ticket_still_authenticates_home_and_panel_api(self):
        ticket = self.ok("POST", "/v1/session/ticket", {})["ticket"]
        self.assertEqual(self.request("POST", "/v1/session/ticket", {}, auth=False)[0], 401)
        code, headers, reply = self.request("POST", "/v1/session/bootstrap", {"ticket": ticket}, auth=False)
        self.assertEqual(code, 200)
        self.assertEqual(reply["data"]["token"], self.TOKEN)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("POST", "/v1/session/bootstrap", {"ticket": ticket}, auth=False)[0], 401)
        self.assertEqual(self.request("GET", "/v1/observatory", headers={"Authorization": "Bearer " + reply["data"]["token"]})[0], 200)
        self.assertEqual(self.request("GET", "/v1/status")[0], 200)
        self.assertEqual(self.request("GET", "/panel", auth=False)[0], 200)

    def test_expired_or_cross_origin_bootstrap_cannot_grant_access(self):
        with patch("xiaojiu_brain.app.time.monotonic", return_value=time.monotonic() - 61):
            expired = self.app.issue_ticket()
        self.assertEqual(self.request("POST", "/v1/session/bootstrap", {"ticket": expired}, auth=False)[0], 401)
        ticket = self.ok("POST", "/v1/session/ticket", {})["ticket"]
        self.assertEqual(self.request("POST", "/v1/session/bootstrap", {"ticket": ticket}, auth=False,
                                      headers={"Origin": "http://foreign.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/v1/session/bootstrap", {"ticket": ticket}, auth=False)[0], 200)

    def test_concurrent_ticket_redemption_has_only_one_winner(self):
        ticket = self.ok("POST", "/v1/session/ticket", {})["ticket"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(lambda _: self.request("POST", "/v1/session/bootstrap", {"ticket": ticket}, auth=False)[0], range(6)))
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(401), 5)

    def test_empty_known_counts_are_zero_but_unknown_usage_is_not_zero(self):
        data = self.observatory()
        self.assertEqual(data["brand"], {"en": "Memory Brain", "zh": "忆核大脑"})
        self.assertEqual(data["storage"]["messages"], 0)
        self.assertEqual(data["pipeline"]["jobs"]["total"], 0)
        self.assertEqual(data["usage"]["attempts"], 0)
        for key in ("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens", "cache_hit_ratio"):
            self.assertIsNone(data["usage"][key], key)

    def test_job_and_storage_counts_belong_only_to_authenticated_owner(self):
        self.seed_message("own")
        for number in range(4):
            self.seed_message("foreign-" + str(number), owner="different-owner")
        one_job = self.app.store.conn.execute("SELECT id FROM jobs WHERE owner=? AND kind='embed'", (self.OWNER,)).fetchone()[0]
        self.app.store.conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (one_job,))
        self.app.store.conn.execute("UPDATE jobs SET status='running' WHERE owner='different-owner'")
        data = self.observatory("?owner=different-owner")
        self.assertEqual(data["storage"]["messages"], 1)
        jobs = data["pipeline"]["jobs"]
        self.assertEqual(jobs["total"], 2)
        self.assertEqual(jobs["by_status"]["pending"], 1)
        self.assertEqual(jobs["by_status"]["failed"], 1)
        self.assertEqual(jobs["by_status"]["running"], 0)

    def test_no_private_content_paths_owner_or_credentials_in_projection(self):
        markers = ["RAW_PRIVATE_MARKER", "SELF_PRIVATE_MARKER", "STYLE_PRIVATE_MARKER",
                   "PAD_NOTES_PRIVATE_MARKER", "PAD_TAG_PRIVATE_MARKER", "BOOK_TITLE_PRIVATE_MARKER",
                   "BOOK_BODY_PRIVATE_MARKER", "EVENT_PRIVATE_MARKER", "TASK_PRIVATE_MARKER"]
        self.seed_message("private", text=markers[0])
        self.app.extensions.update_self({"text": markers[1], "style_preferences": markers[2]})
        self.app.extensions.update_emotion({"pleasure": .4, "notes": markers[3], "tags": [markers[4]]})
        self.app.extensions.update_settings({"life_enabled": True, "emotion_enabled": True, "self_notes_enabled": True})
        self.app.life.add_book({"title": markers[5], "text": markers[6]})
        self.app.life.add_event({"text": markers[7]})
        self.app.life.add_task({"title": markers[8]})
        self.app.life.tick("2026-05-02T10:00:00+08:00")
        projection = self.observatory()
        serialized = json.dumps(projection, ensure_ascii=False)
        for marker in markers + [self.TOKEN, self.OWNER, self.tmp.name]:
            self.assertNotIn(marker, serialized)
        def all_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from all_keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from all_keys(child)
        forbidden = {"api_token", "owner", "owner_id", "data_dir", "notes", "tags",
                     "style_preferences", "current", "text", "quote", "source_refs", "metadata"}
        self.assertFalse(set(all_keys(projection)) & forbidden)

    def test_cache_ratio_uses_known_backend_usage_and_excludes_other_owner(self):
        self.seed_usage(usage={"input_tokens": 100, "output_tokens": 20,
                               "cache_hit_tokens": 75, "cache_miss_tokens": 25, "total_tokens": 120})
        self.seed_usage(ident="other-usage", owner="different-owner",
                        usage={"input_tokens": 10000, "cache_hit_tokens": 0, "cache_miss_tokens": 10000})
        usage = self.observatory()["usage"]
        self.assertEqual(usage["attempts"], 1)
        self.assertEqual(usage["cache_hit_ratio"], .75)
        self.assertEqual(usage["input_tokens"], 100)
        self.assertIn("后台", json.dumps(usage, ensure_ascii=False))

    def test_known_zero_cache_denominator_is_not_a_zero_percent_hit_rate(self):
        self.seed_usage(usage={"input_tokens": 0, "output_tokens": 0,
                               "cache_hit_tokens": 0, "cache_miss_tokens": 0})
        usage = self.observatory()["usage"]
        self.assertEqual(usage["input_tokens"], 0)
        self.assertEqual(usage["cache_hit_tokens"], 0)
        self.assertIsNone(usage["cache_hit_ratio"])

    def test_partial_or_unknown_usage_does_not_invent_missing_cache_values(self):
        self.seed_usage(usage={"input_tokens": 100, "cache_hit_tokens": 25})
        self.seed_usage(ident="unreported", usage=None)
        usage = self.observatory()["usage"]
        self.assertEqual(usage["attempts"], 2)
        self.assertEqual(usage["unknown_usage_attempts"], 1)
        self.assertIsNone(usage["cache_miss_tokens"])
        self.assertIsNone(usage["cache_hit_ratio"])

    def test_observatory_polling_does_not_generate_work_activity_or_paid_calls(self):
        self.seed_message("seed")
        before = self.table_counts()
        changes_before = self.app.store.conn.total_changes
        with patch.object(self.app.llm, "complete_json", side_effect=AssertionError("unexpected LLM call")) as llm_call, \
             patch.object(self.app.embedder, "embed_many", side_effect=AssertionError("unexpected embedding call")) as embedding_call:
            first = self.observatory()
            self.observatory()
            self.observatory()
        llm_call.assert_not_called()
        embedding_call.assert_not_called()
        self.assertEqual(self.table_counts(), before)
        self.assertEqual(self.app.store.conn.total_changes, changes_before)
        self.assertEqual(first["usage"]["attempts"], 0)

    def test_unreadable_life_counts_are_unknown_instead_of_fabricated_zero(self):
        with patch.object(self.app.life, "snapshot", side_effect=OSError("synthetic unreadable state")):
            data = self.observatory()
        self.assertEqual(data["storage"]["messages"], 0)
        self.assertIsNotNone(data["life"]["error"])
        self.assertTrue(all(value is None for value in data["life"]["counts"].values()))
        life_node = next(node for node in data["scene"]["nodes"] if node["id"] == "life")
        self.assertIsNone(life_node["count"])

    def test_existing_management_and_life_api_still_return_authenticated_data(self):
        self.assertEqual(self.request("GET", "/v1/status")[0], 200)
        self.assertEqual(self.request("GET", "/v1/settings")[0], 200)
        self.assertEqual(self.request("GET", "/v1/life")[0], 200)
        self.assertEqual(self.request("GET", "/v1/observatory/unknown")[0], 404)

    def test_activity_counts_only_valid_owner_work_and_six_second_past_window(self):
        now = 1_800_000_000.
        for number in range(6):
            self.seed_message("activity-own-" + str(number))
        self.seed_message("activity-foreign", owner="different-owner")
        self.app.store.conn.execute("UPDATE jobs SET status='done',updated_at=?,lease_until=0 WHERE owner=?",
                                    (now - 60, self.OWNER))
        by_kind = {kind: [row[0] for row in self.app.store.conn.execute(
            "SELECT id FROM jobs WHERE owner=? AND kind=? ORDER BY id", (self.OWNER, kind))]
            for kind in ("derive", "embed")}
        fixtures = [
            (by_kind["derive"][0], "running", now - 1, now + 20),
            (by_kind["embed"][0], "running", now - 2, now + 20),
            (by_kind["derive"][1], "running", now - 3, now - 1),
            (by_kind["embed"][1], "running", now + 1, now + 20),
            (by_kind["derive"][2], "pending", now - 1, 0),
            (by_kind["embed"][2], "retry", now - 2, 0),
            (by_kind["derive"][3], "done", now - 2, 0),
            (by_kind["embed"][3], "done", now - 6, 0),
            (by_kind["derive"][4], "done", now - 6.1, 0),
            (by_kind["embed"][4], "done", now + .1, 0),
        ]
        for ident, status, updated, lease in fixtures:
            self.app.store.conn.execute("UPDATE jobs SET status=?,updated_at=?,lease_until=? WHERE id=?",
                                        (status, updated, lease, ident))
        self.app.store.conn.execute("UPDATE jobs SET status='running',updated_at=?,lease_until=? WHERE owner='different-owner'",
                                    (now - 1, now + 20))
        with self.app._trace_lock:
            for ident, owner, stamp in [
                ("recent", self.OWNER, now - .25), ("boundary", self.OWNER, now - 6),
                ("old", self.OWNER, now - 6.1), ("future", self.OWNER, now + .1),
                ("foreign", "different-owner", now - .1),
            ]:
                self.app._traces[ident] = {"owner": owner, "at": stamp, "query": "PRIVATE_TRACE_QUERY"}
        with patch("xiaojiu_brain.observatory.time.time", return_value=now):
            data = self.ok("GET", "/v1/activity")
        self.assertEqual(data["kind"], "memory_task_activity")
        self.assertEqual(data["window_seconds"], 6)
        self.assertEqual(data["state"], "busy")
        self.assertEqual(data["running"], {"derive": 1, "embed": 1})
        self.assertEqual(data["recent"], {"recalls": 2, "completed_jobs": 2})
        self.assertEqual(data["expired_running"], 1)
        self.assertEqual(data["waiting"], 3)
        self.assertEqual(data["last_event_at"], now - .25)
        self.assertEqual(data["chat_model_state"], "unobserved")
        self.assertEqual(data["representation"], "decorative_work_signal")

    def test_activity_states_distinguish_idle_recent_waiting_and_live_work(self):
        now = 1_800_000_000.
        self.seed_message("activity-state")
        ids = [row[0] for row in self.app.store.conn.execute("SELECT id FROM jobs WHERE owner=?", (self.OWNER,))]
        self.app.store.conn.execute("UPDATE jobs SET status='done',updated_at=? WHERE owner=?", (now - 60, self.OWNER))
        with patch("xiaojiu_brain.observatory.time.time", return_value=now):
            idle = self.ok("GET", "/v1/activity")
            self.assertEqual(idle["state"], "idle")
            self.assertIsNone(idle["last_event_at"])
            for status, updated, lease, expected in [
                ("done", now - 1, 0, "recent"),
                ("pending", now - 1, 0, "waiting"),
                ("running", now - 1, now - .1, "waiting"),
                ("running", now - 1, now + 1, "busy"),
            ]:
                with self.subTest(status=status, expected=expected):
                    self.app.store.conn.execute("UPDATE jobs SET status=?,updated_at=?,lease_until=? WHERE id=?",
                                                (status, updated, lease, ids[0]))
                    self.assertEqual(self.ok("GET", "/v1/activity")["state"], expected)

    def test_activity_auth_owner_and_private_trace_text_are_not_disclosed(self):
        self.assertEqual(self.request("GET", "/v1/activity", auth=False)[0], 401)
        for headers in ({"Origin": "https://foreign.invalid"}, {"Host": "foreign.invalid"}):
            self.assertEqual(self.request("GET", "/v1/activity", headers=headers)[0], 403)
        with self.app._trace_lock:
            self.app._traces["private-trace"] = {"owner": self.OWNER, "at": time.time(),
                "conversation_id": "PRIVATE_CONVERSATION", "query": "PRIVATE_QUERY",
                "evidence": [{"text": "PRIVATE_EVIDENCE"}], "notes": "PRIVATE_NOTES"}
            self.app._traces["foreign-trace"] = {"owner": "PRIVATE_FOREIGN_OWNER", "at": time.time(),
                "query": "PRIVATE_FOREIGN_QUERY"}
        body = self.ok("GET", "/v1/activity?owner=PRIVATE_FOREIGN_OWNER")
        self.assertEqual(body["recent"]["recalls"], 1)
        serialized = json.dumps(body)
        for secret in (self.TOKEN, self.OWNER, self.tmp.name, "PRIVATE_CONVERSATION", "PRIVATE_QUERY",
                       "PRIVATE_EVIDENCE", "PRIVATE_NOTES", "PRIVATE_FOREIGN_OWNER", "PRIVATE_FOREIGN_QUERY"):
            self.assertNotIn(secret, serialized)

    def test_repeated_activity_reads_have_no_paid_or_durable_side_effect(self):
        self.seed_message("activity-readonly")
        counts, changes = self.table_counts(), self.app.store.conn.total_changes
        with patch.object(self.app.llm, "complete_json", side_effect=AssertionError("unexpected LLM call")) as llm_call, \
             patch.object(self.app.embedder, "embed_many", side_effect=AssertionError("unexpected vector call")) as embedding_call:
            for _ in range(4):
                self.ok("GET", "/v1/activity")
        llm_call.assert_not_called()
        embedding_call.assert_not_called()
        self.assertEqual(self.table_counts(), counts)
        self.assertEqual(self.app.store.conn.total_changes, changes)

    def test_activity_read_failure_reports_unknown_counts_without_private_error(self):
        with patch("xiaojiu_brain.observatory._activity_jobs", side_effect=OSError("PRIVATE_ERROR_PATH " + self.tmp.name)):
            data = self.ok("GET", "/v1/activity")
        self.assertEqual(data["state"], "unavailable")
        self.assertEqual(data["running"], {"derive": None, "embed": None})
        self.assertEqual(data["recent"], {"recalls": None, "completed_jobs": None})
        self.assertIsNone(data["waiting"])
        self.assertIsNone(data["expired_running"])
        self.assertIsNone(data["last_event_at"])
        self.assertEqual(data["chat_model_state"], "unobserved")
        self.assertNotIn("PRIVATE_ERROR_PATH", json.dumps(data))
        self.assertNotIn(self.tmp.name, json.dumps(data))


if __name__ == "__main__":
    unittest.main()
