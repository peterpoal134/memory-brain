"""Durable browser login and real local HTTP auth; synthetic credentials only."""
import copy
from http.cookies import SimpleCookie
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from xiaojiu_brain.api import BrainServer
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.browser_auth import BrowserSessions
from xiaojiu_brain.config import Config


class BrowserSessionStorageTests(unittest.TestCase):
    KEY = "synthetic-management-key-0123456789"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "browser-sessions.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_hash_and_metadata_persist_and_validation_never_writes(self):
        sessions = BrowserSessions(self.path, self.KEY)
        raw, expires = sessions.issue()
        content = self.path.read_bytes()
        persisted = json.loads(content)
        self.assertRegex(raw, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotIn(raw.encode(), content)
        self.assertNotIn(self.KEY.encode(), content)
        self.assertEqual(len(persisted["sessions"]), 1)
        digest, entry = next(iter(persisted["sessions"].items()))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(set(entry), {"created_at", "expires_at"})
        self.assertEqual(entry["expires_at"], expires)
        stat = self.path.stat()
        with patch.object(sessions, "_save", side_effect=AssertionError("auth read must not persist")):
            for _ in range(3):
                self.assertTrue(sessions.valid(raw))
                for wrong in (None, "", "A" * 43, raw + "x", self.KEY):
                    self.assertFalse(sessions.valid(wrong))
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(self.path.stat().st_mtime_ns, stat.st_mtime_ns)

    def test_absolute_expiry_survives_reload_and_key_rotation_invalidates_sessions(self):
        now = [1_800_000_000.]
        with patch("xiaojiu_brain.browser_auth.time.time", side_effect=lambda: now[0]):
            sessions = BrowserSessions(self.path, self.KEY, ttl=10)
            raw, expires = sessions.issue()
            now[0] += 9
            restored = BrowserSessions(self.path, self.KEY, ttl=10)
            self.assertTrue(restored.valid(raw))
            self.assertEqual(next(iter(restored.sessions.values()))["expires_at"], expires)
            now[0] += 1
            self.assertFalse(restored.valid(raw))
            self.assertFalse(BrowserSessions(self.path, self.KEY, ttl=10).valid(raw))
            new_raw, _ = restored.issue()
            self.assertTrue(BrowserSessions(self.path, self.KEY, ttl=10).valid(new_raw))
            rotated = BrowserSessions(self.path, self.KEY + "-rotated", ttl=10)
            self.assertFalse(rotated.valid(new_raw))
            self.assertEqual(rotated.sessions, {})

    def test_corrupt_or_invalid_cache_fails_closed_without_repairing_on_auth_read(self):
        sessions = BrowserSessions(self.path, self.KEY)
        raw, _ = sessions.issue()
        original = json.loads(self.path.read_text(encoding="utf-8"))
        variants = ["{broken-json", "[]"]
        for field, value in (("created_at", True), ("expires_at", float("nan"))):
            bad = copy.deepcopy(original)
            next(iter(bad["sessions"].values()))[field] = value
            variants.append(json.dumps(bad))
        wrong_binding = copy.deepcopy(original)
        wrong_binding["binding"] = "0" * 64
        variants.append(json.dumps(wrong_binding))
        for content in variants:
            with self.subTest(content=content[:35]):
                self.path.write_text(content, encoding="utf-8")
                before = self.path.read_bytes()
                rejected = BrowserSessions(self.path, self.KEY)
                self.assertFalse(rejected.valid(raw))
                self.assertEqual(rejected.sessions, {})
                self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_save_failure_does_not_commit_issue_or_claim_successful_revoke(self):
        sessions = BrowserSessions(self.path, self.KEY)
        with patch.object(sessions, "_save", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                sessions.issue()
        self.assertEqual(sessions.sessions, {})
        self.assertFalse(self.path.exists())
        raw, _ = sessions.issue()
        before = self.path.read_bytes()
        with patch.object(sessions, "_save", side_effect=OSError("synthetic revoke failure")):
            with self.assertRaises(OSError):
                sessions.revoke(raw)
        self.assertTrue(sessions.valid(raw), "a failed revoke must not pretend durable success")
        self.assertEqual(self.path.read_bytes(), before)
        sessions.revoke(raw)
        self.assertFalse(sessions.valid(raw))
        self.assertFalse(BrowserSessions(self.path, self.KEY).valid(raw))


class BrowserAuthHTTPTests(unittest.TestCase):
    TOKEN = "synthetic-browser-http-manager-0123456789"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=self.tmp.name, owner="synthetic-owner", api_token=self.TOKEN)
        self.start()

    def start(self, port=0):
        self.app = BrainApp(self.config, start_worker=False)
        self.server = BrainServer(("127.0.0.1", port), self.app)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.app.close()

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, *, bearer=False, cookie=None, headers=None):
        given = {"Authorization": "Bearer " + self.TOKEN} if bearer else {}
        if cookie:
            given["Cookie"] = cookie
        given.update(headers or {})
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            given["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=encoded, headers=given)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw)
        finally:
            connection.close()

    def cookie_pair(self, headers):
        jar = SimpleCookie()
        jar.load(headers["Set-Cookie"])
        entry = jar[self.server.browser_cookie_name]
        return entry.key + "=" + entry.value

    def login_cookie(self):
        code, headers, body = self.request("POST", "/v1/session/browser", {}, bearer=True)
        self.assertEqual(code, 200, body)
        return self.cookie_pair(headers)

    def test_no_anonymous_cookie_wrong_cookie_rejected_and_bearer_still_works(self):
        name = self.server.browser_cookie_name
        for method, path, body in [
            ("GET", "/v1/session/current", None),
            ("GET", "/v1/status", None),
            ("POST", "/v1/session/browser", {}),
            ("POST", "/v1/session/logout", {}),
        ]:
            for cookie in (None, name + "=" + "A" * 43):
                with self.subTest(path=path, cookie_present=bool(cookie)):
                    code, headers, _ = self.request(method, path, body, cookie=cookie)
                    self.assertEqual(code, 401)
                    self.assertNotIn("Set-Cookie", headers)
        code, _, body = self.request("GET", "/v1/session/current", bearer=True)
        self.assertEqual(code, 200)
        self.assertEqual(body["data"], {"authenticated": True, "mode": "bearer"})
        self.assertEqual(self.request("GET", "/v1/status", bearer=True)[0], 200)

    def test_ticket_bootstrap_sets_safe_persistent_cookie_and_remains_single_use(self):
        code, _, body = self.request("POST", "/v1/session/ticket", {}, bearer=True)
        self.assertEqual(code, 200)
        ticket = body["data"]["ticket"]
        code, headers, body = self.request("POST", "/v1/session/bootstrap", {"ticket": ticket})
        self.assertEqual(code, 200)
        self.assertEqual(body["data"]["token"], self.TOKEN, "legacy bootstrap compatibility changed")
        jar = SimpleCookie()
        jar.load(headers["Set-Cookie"])
        entry = jar[self.server.browser_cookie_name]
        self.assertTrue(entry["httponly"])
        self.assertEqual(entry["samesite"], "Strict")
        self.assertEqual(entry["path"], "/v1")
        self.assertEqual(entry["max-age"], str(BrowserSessions.TTL))
        self.assertEqual(entry["domain"], "")
        self.assertFalse(entry["secure"], "loopback HTTP cannot use a Secure-only cookie")
        cookie = self.cookie_pair(headers)
        self.assertEqual(self.request("GET", "/v1/status", cookie=cookie)[0], 200)
        code, repeated_headers, _ = self.request("POST", "/v1/session/bootstrap", {"ticket": ticket})
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", repeated_headers)

        _, _, second_ticket = self.request("POST", "/v1/session/ticket", {}, bearer=True)
        sidecar = self.app.data_dir / "browser-sessions.json"
        unchanged = sidecar.read_bytes()
        with patch.object(self.server.browser_sessions, "_save", side_effect=AssertionError("valid cookie must be reused")):
            code, reused_headers, _ = self.request("POST", "/v1/session/bootstrap",
                {"ticket": second_ticket["data"]["ticket"]}, cookie=cookie)
        self.assertEqual(code, 200)
        self.assertNotIn("Set-Cookie", reused_headers)
        self.assertEqual(sidecar.read_bytes(), unchanged)
        self.assertEqual(self.request("POST", "/v1/session/bootstrap",
            {"ticket": second_ticket["data"]["ticket"]}, cookie=cookie)[0], 401)
        self.assertEqual(self.request("GET", "/v1/session/current", cookie=cookie)[0], 200)

    def test_current_and_browser_resumption_do_not_disclose_key_or_accept_wrong_bearer(self):
        original = self.login_cookie()
        code, _, current = self.request("GET", "/v1/session/current", cookie=original)
        self.assertEqual(code, 200)
        self.assertEqual(current["data"], {"authenticated": True, "mode": "browser"})
        self.assertNotIn(self.TOKEN, json.dumps(current))
        self.assertEqual(self.request("GET", "/v1/status", cookie=original,
                                      headers={"Authorization": "Bearer definitely-wrong"})[0], 401)
        sidecar = self.app.data_dir / "browser-sessions.json"
        before = sidecar.read_bytes()
        with patch.object(self.server.browser_sessions, "_save", side_effect=AssertionError("session read must not rotate")):
            code, headers, resumed = self.request("POST", "/v1/session/browser", {}, cookie=original)
        self.assertEqual(code, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertNotIn(self.TOKEN, json.dumps(resumed))
        self.assertEqual(self.request("GET", "/v1/session/current", cookie=original)[0], 200)

    def test_cookie_survives_same_port_restart_and_logout_revocation_is_durable(self):
        cookie = self.login_cookie()
        port = self.port
        self.stop()
        self.start(port)
        self.assertEqual(self.request("GET", "/v1/session/current", cookie=cookie)[0], 200)
        code, headers, body = self.request("POST", "/v1/session/logout", {}, cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(body["data"]["logged_out"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/v1/status", cookie=cookie)[0], 401)
        self.assertEqual(self.request("GET", "/v1/status", bearer=True)[0], 200,
                         "browser logout must not revoke the plugin bearer")
        self.stop()
        self.start(port)
        self.assertEqual(self.request("GET", "/v1/session/current", cookie=cookie)[0], 401)

    def test_cookie_cannot_bypass_cross_origin_or_host_guards_or_trigger_csrf_logout(self):
        cookie = self.login_cookie()
        for method, path, body in [
            ("GET", "/v1/session/current", None),
            ("POST", "/v1/session/browser", {}),
            ("POST", "/v1/session/logout", {}),
        ]:
            for attack in ({"Origin": "https://foreign.invalid"}, {"Origin": "null"}, {"Host": "foreign.invalid"}):
                with self.subTest(path=path, attack=attack):
                    code, headers, _ = self.request(method, path, body, cookie=cookie, headers=attack)
                    self.assertEqual(code, 403)
                    self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request("GET", "/v1/session/current", cookie=cookie)[0], 200)

    def test_http_persistence_failure_is_visible_and_never_acknowledges_login_or_logout(self):
        with patch.object(self.server.browser_sessions, "_save", side_effect=OSError("PRIVATE_FAILURE_DETAIL")):
            code, headers, body = self.request("POST", "/v1/session/browser", {}, bearer=True)
        self.assertEqual(code, 500)
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("PRIVATE_FAILURE_DETAIL", json.dumps(body))
        self.assertEqual(self.server.browser_sessions.sessions, {})
        cookie = self.login_cookie()
        with patch.object(self.server.browser_sessions, "_save", side_effect=OSError("PRIVATE_FAILURE_DETAIL")):
            code, headers, body = self.request("POST", "/v1/session/logout", {}, cookie=cookie)
        self.assertEqual(code, 500)
        self.assertFalse(body["ok"])
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("PRIVATE_FAILURE_DETAIL", json.dumps(body))
        self.assertEqual(self.request("GET", "/v1/session/current", cookie=cookie)[0], 200)


if __name__ == "__main__":
    unittest.main()
