"""Dashboard still serves, and the update endpoints keep the host and CSRF checks."""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import dashboard
import updater

ROOT = Path(__file__).resolve().parents[1]


def _request(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class DashboardUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dashboard.PASSWORD = ""
        dashboard.SESSIONS.clear()
        cls.server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_dashboard_starts_and_shows_version(self):
        status, body = _request("GET", self.base + "/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Grok Bot", text)
        self.assertIn("Check for updates", text)
        self.assertIn("Roll back", text)
        self.assertIn("v" + (ROOT / "VERSION").read_text(encoding="utf-8").strip(), text)
        self.assertIn("BYBIT:SOLUSDT.P", text)
        self.assertIn("Asia/Manila", text)
        self.assertIn("https://s3.tradingview.com/tv.js", text)
        self.assertIn("Bot chart (with trade levels)", text)
        self.assertIn("M213.235 306.019", text)
        self.assertIn("Grok Bot", text)
        self.assertNotIn("upload.wikimedia.org", text)
        self.assertNotIn("M22 11.5A8", text)
        import http.client
        port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/", headers={"Host": "127.0.0.1"})
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
        conn.close()
        self.assertNotIn("content-security-policy", headers)
        self.assertIn("Learning", text)
        self.assertIn("Bot chart (with trade levels)", text)
        status, body = _request("GET", self.base + "/api/learning")
        self.assertEqual(status, 200)
        view = json.loads(body)
        self.assertIn("summary", view)
        self.assertIn("proposal", view)
        self.assertIn("buckets", view)
        status, body = _request("GET", self.base + "/api/about")
        self.assertEqual(status, 200)
        about = json.loads(body)
        self.assertEqual(about["version"], "1.0.0")
        self.assertIn("blocked", about)
        self.assertIn("backups", about)

    def test_host_and_csrf_checks_apply_to_update(self):
        import http.client
        port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/about", headers={"Host": "evil.example"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        resp.read()
        conn.request("POST", "/api/update/apply", body=b"{}", headers={
            "Host": "127.0.0.1", "Content-Type": "application/json",
        })
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        self.assertIn(b"X-Dashboard", resp.read())
        conn.close()

        status, body = _request("POST", self.base + "/api/update/apply", {}, {
            "Content-Type": "application/json", "X-Dashboard": "1",
        })
        self.assertEqual(status, 400)
        self.assertIn(b"confirm", body)

        status, body = _request("POST", self.base + "/api/update/rollback", {}, {
            "Content-Type": "application/json", "X-Dashboard": "1",
        })
        self.assertEqual(status, 400)
        self.assertIn(b"confirm", body)

    def test_apply_is_refused_with_an_open_position(self):
        called = {}

        def boom(*args, **kwargs):
            called["yes"] = True
            raise AssertionError("update should not start")

        old = dashboard.updater.perform_update
        old_status = dashboard.STATUS_FILE
        status_path = ROOT / "state" / "status-test.json"
        status_path.parent.mkdir(exist_ok=True)
        status_path.write_text(json.dumps({
            "running": True,
            "heartbeat_epoch": time.time(),
            "mode": "LIVE",
            "position": {"side": "Buy", "size": "1"},
            "pending_order": None,
        }), encoding="utf-8")
        dashboard.updater.perform_update = boom
        dashboard.STATUS_FILE = status_path
        try:
            status, body = _request("POST", self.base + "/api/update/apply", {"confirm": True}, {
                "Content-Type": "application/json", "X-Dashboard": "1",
            })
        finally:
            dashboard.updater.perform_update = old
            dashboard.STATUS_FILE = old_status
            status_path.unlink(missing_ok=True)
        self.assertEqual(status, 400)
        self.assertIn(b"Stop the bot", body)
        self.assertNotIn("yes", called)

    def test_confirm_runs_the_job_against_the_patched_updater(self):
        def fake_update(root, **kwargs):
            return updater.ApplyResult(ok=True, summary="Updated to v9", output="done-marker", version="9")

        old = dashboard.updater.perform_update
        dashboard.updater.perform_update = fake_update
        try:
            status, body = _request("POST", self.base + "/api/update/apply", {"confirm": True}, {
                "Content-Type": "application/json", "X-Dashboard": "1",
            })
            self.assertEqual(status, 200)
            job_id = json.loads(body)["job"]
            payload = {}
            for _ in range(50):
                status, raw = _request("GET", self.base + "/api/job?id=" + job_id)
                payload = json.loads(raw)
                if payload.get("status") != "running":
                    break
                time.sleep(0.05)
        finally:
            dashboard.updater.perform_update = old
        self.assertEqual(payload.get("status"), "done")
        self.assertIn("done-marker", payload.get("output", ""))
        self.assertFalse(payload.get("restarting"))

    def test_check_endpoint_uses_csrf_header(self):
        def fake_check(root, **kwargs):
            return {"local_version": "1.0.0", "remote_version": "1.2.0", "update_available": True,
                    "commits": [{"sha": "abc", "message": "Fix stops", "date": "2026-11-18T00:00:00Z"}],
                    "changelog": "## 1.2.0\n\n- Fix stops", "error": None}

        old = dashboard.updater.check_for_updates
        dashboard.updater.check_for_updates = fake_check
        try:
            status, body = _request("POST", self.base + "/api/update/check", {}, {
                "Content-Type": "application/json", "X-Dashboard": "1",
            })
        finally:
            dashboard.updater.check_for_updates = old
        self.assertEqual(status, 200)
        info = json.loads(body)
        self.assertTrue(info["update_available"])
        self.assertEqual(info["commits"][0]["message"], "Fix stops")


if __name__ == "__main__":
    unittest.main()
