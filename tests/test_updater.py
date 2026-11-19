"""Updater tests. Network and pip are faked; zipballs are built in memory."""
from __future__ import annotations

import io
import json
import subprocess
import unittest
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

import updater

ROOT = Path(__file__).resolve().parents[1]
OLD = "1111111111111111111111111111111111111111"
NEW = "abcdef1234567890abcdef1234567890abcd1234"
TOKEN = "ghp_supersecretvalue"


def _resp(body, status=200):
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode()
    return updater.FetchResponse(status, body, {}, "")


def _zip(files: dict[str, bytes | str], prefix: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            if isinstance(data, str):
                data = data.encode()
            path = f"{prefix}{name}" if prefix else name
            zf.writestr(path, data)
    return buf.getvalue()


class UpdateAvailableTests(unittest.TestCase):
    def test_versions_and_commits(self):
        self.assertFalse(updater.is_update_available("1.0.0", "1.0.0", None, NEW))
        self.assertTrue(updater.is_update_available("1.0.0", "1.1.0", None, NEW))
        self.assertTrue(updater.is_update_available("unknown", "1.0.0", None, NEW))
        self.assertFalse(updater.is_update_available("1.0.0", "1.0.0", NEW, NEW))
        self.assertFalse(updater.is_update_available("9.9.9", "1.0.0", NEW, NEW))  # same commit wins
        self.assertTrue(updater.is_update_available("1.0.0", "1.0.0", OLD, NEW))
        self.assertTrue(updater.is_update_available("1.0.0", "1.0.0", OLD[:12], NEW))

    def test_changelog_stops_at_installed_version(self):
        text = "# Changelog\n\n## 1.1.0 - 2026-11-19\n\n- Added a thing\n\n## 1.0.0 - 2026-11-18\n\n- Initial\n"
        excerpt = updater.changelog_excerpt(text, "1.0.0")
        self.assertIn("Added a thing", excerpt)
        self.assertNotIn("Initial", excerpt)

    def test_trading_block_only_when_running_with_exposure(self):
        self.assertIsNone(updater.trading_block_reason({"alive": False, "position": {"side": "Buy"}}))
        self.assertIsNone(updater.trading_block_reason({"alive": True, "position": None, "pending_order": None}))
        both = updater.trading_block_reason({"alive": True, "position": {"side": "Buy"}, "pending_order": {"id": "1"}})
        self.assertIn("open position", both)
        self.assertIn("pending order", both)
        self.assertIn("Stop the bot", both)
        self.assertIn("open position", updater.trading_block_reason({"alive": True, "position": {"side": "Sell"}}))
        self.assertIn("pending order", updater.trading_block_reason({"alive": True, "pending_order": {"id": "1"}}))


class ConfigMergeTests(unittest.TestCase):
    def test_real_config_keys_round_trip(self):
        text = (ROOT / "config.yaml").read_text(encoding="utf-8")
        found = updater.iter_yaml_keys(text)
        declared = []
        for line in text.splitlines():
            m = updater._KEY_RE.match(line)
            if m and not line.strip().startswith("#"):
                declared.append(m.group(2))
        self.assertEqual(len(found), len(declared))
        self.assertIn("risk.risk_usdt", found)
        self.assertIn("strategy.rr", found)
        same, added = updater.merge_config_text(text, text)
        self.assertEqual(same, text)
        self.assertEqual(added, [])

    def test_keeps_user_values_and_inserts_new_keys(self):
        original = (ROOT / "config.yaml").read_text(encoding="utf-8")
        user = original.replace("dry_run: true", "dry_run: false", 1).replace("risk_usdt: 5.0", "risk_usdt: 7.5", 1)
        new = original.replace(
            "  leverage: 5                  # set via API on start; isolated margin\n",
            "  leverage: 5                  # set via API on start; isolated margin\n  spread_cap: 0.1\n",
            1,
        )
        new = new.rstrip("\n") + "\n\n# release channel\nupdate_channel: stable\n"
        merged, added = updater.merge_config_text(user, new)
        loaded = yaml.safe_load(merged)
        self.assertIs(loaded["dry_run"], False)
        self.assertEqual(loaded["risk"]["risk_usdt"], 7.5)
        self.assertEqual(loaded["risk"]["spread_cap"], 0.1)
        self.assertEqual(loaded["risk"]["leverage"], 5)
        self.assertEqual(loaded["update_channel"], "stable")
        self.assertEqual(loaded["strategy"]["rr"], 2.0)
        self.assertIn("would-be orders", merged)
        self.assertIn("risk.spread_cap", added)
        self.assertIn("update_channel", added)
        self.assertNotIn("risk.risk_usdt", added)

    def test_preserves_crlf(self):
        user = "dry_run: false\r\nrisk:\r\n  risk_usdt: 7.5\r\n"
        new = "dry_run: true\nrisk:\n  risk_usdt: 5.0\n  extra: 1\n"
        merged, added = updater.merge_config_text(user, new)
        self.assertIn("\r\n", merged)
        self.assertIn("dry_run: false", merged)
        self.assertIn("risk_usdt: 7.5", merged)
        self.assertIn("extra: 1", merged)
        self.assertEqual(added, ["risk.extra"])


class ZipApplyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.prefix = f"bybit-sol-smc-bot-{NEW}/"

    def tearDown(self):
        self._td.cleanup()

    def _project(self):
        (self.root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (self.root / "bot.py").write_text("print('old')\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text("pybit==1.0\n", encoding="utf-8")
        (self.root / "config.yaml").write_text("dry_run: false\nrisk:\n  risk_usdt: 7.5\n", encoding="utf-8")
        (self.root / "config.yaml.bak").write_text("user backup\n", encoding="utf-8")
        (self.root / ".env").write_text("BYBIT_API_KEY=secret-key\nGITHUB_TOKEN=" + TOKEN + "\n", encoding="utf-8")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "bot.log").write_text("keep-log\n", encoding="utf-8")
        (self.root / "state").mkdir()
        (self.root / "state" / "x.json").write_text('{"keep": true}\n', encoding="utf-8")
        (self.root / "state" / "update.json").write_text(
            json.dumps({"version": "1.0.0", "commit": OLD}) + "\n", encoding="utf-8")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "pyvenv.cfg").write_text("home = kept\n", encoding="utf-8")

    def _remote_files(self):
        return {
            "VERSION": "1.1.0\n",
            "bot.py": "print('new')\n",
            "extra.py": "print('extra')\n",
            "requirements.txt": "pybit==2.0\n",
            "config.yaml": "dry_run: true\nrisk:\n  risk_usdt: 5.0\n  spread_cap: 0.1\nextra_flag: steady\n",
            ".env": "BYBIT_API_KEY=EVIL\n",
            "logs/bot.log": "EVIL\n",
            "state/x.json": "EVIL\n",
            ".venv/pyvenv.cfg": "EVIL\n",
            "config.yaml.bak": "EVIL\n",
            "backtest_trades.csv": "EVIL\n",
        }

    def test_apply_preserves_user_files_and_rollback_restores(self):
        self._project()
        data = _zip(self._remote_files(), self.prefix)
        calls = []

        def pip(root):
            calls.append(root)
            return True, "Successfully installed pybit-2"

        manifest = updater.apply_zip_bytes(self.root, data, commit=NEW)
        self.assertTrue(manifest["requirements_changed"])
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "print('new')\n")
        self.assertEqual((self.root / "extra.py").read_text(encoding="utf-8"), "print('extra')\n")
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "1.1.0\n")
        self.assertIn("secret-key", (self.root / ".env").read_text(encoding="utf-8"))
        self.assertEqual((self.root / "logs" / "bot.log").read_text(encoding="utf-8"), "keep-log\n")
        self.assertEqual((self.root / "state" / "x.json").read_text(encoding="utf-8"), '{"keep": true}\n')
        self.assertEqual((self.root / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8"), "home = kept\n")
        self.assertEqual((self.root / "config.yaml.bak").read_text(encoding="utf-8"), "user backup\n")
        self.assertFalse((self.root / "backtest_trades.csv").exists())
        loaded = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
        self.assertIs(loaded["dry_run"], False)
        self.assertEqual(loaded["risk"]["risk_usdt"], 7.5)
        self.assertEqual(loaded["risk"]["spread_cap"], 0.1)
        self.assertEqual(loaded["extra_flag"], "steady")
        stamp = json.loads((self.root / "state" / "update.json").read_text(encoding="utf-8"))
        self.assertEqual(stamp["commit"], NEW)
        self.assertEqual(stamp["version"], "1.1.0")
        backup = self.root / "backups" / manifest["id"]
        self.assertEqual((backup / "bot.py").read_text(encoding="utf-8"), "print('old')\n")
        self.assertIn("risk.spread_cap", manifest["config_keys_added"])

        result = updater.rollback(self.root, manifest["id"], pip_runner=pip, status_fn=lambda: {"alive": False})
        self.assertTrue(result.ok)
        self.assertEqual(calls, [self.root])
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "print('old')\n")
        self.assertFalse((self.root / "extra.py").exists())
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "1.0.0\n")
        self.assertIn("secret-key", (self.root / ".env").read_text(encoding="utf-8"))
        self.assertEqual((self.root / "logs" / "bot.log").read_text(encoding="utf-8"), "keep-log\n")
        restored = json.loads((self.root / "state" / "update.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["commit"], OLD)
        self.assertIn("Rolled back", result.output)
        self.assertIn("Successfully installed", result.output)

    def test_unsafe_paths_and_symlink_are_refused(self):
        self._project()
        for name in ("../evil.py", "foo/../../evil.py", "/tmp/evil.py", "C:/Windows/evil.py"):
            with self.assertRaises(updater.UpdateError):
                updater.apply_zip_bytes(self.root, _zip({name: "pwn"}))
        self.assertFalse((self.root.parent / "evil.py").exists())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo(f"bybit-sol-smc-bot-{NEW}/link")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
        with self.assertRaises(updater.UpdateError):
            updater.apply_zip_bytes(self.root, buf.getvalue())
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "print('old')\n")

    def test_rollback_rejects_manifest_escape(self):
        self._project()
        bid = "20260101T000000Z"
        bdir = self.root / "backups" / bid
        (bdir / "bot.py").parent.mkdir(parents=True)
        (bdir / "bot.py").write_text("x", encoding="utf-8")
        (bdir / "manifest.json").write_text(json.dumps({
            "id": bid, "replaced": ["../evil.py"], "added": [], "had_update_meta": False,
        }), encoding="utf-8")
        with self.assertRaises(updater.UpdateError):
            updater.rollback(self.root, bid, status_fn=lambda: {"alive": False})
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "print('old')\n")


class PerformUpdateTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (self.root / "bot.py").write_text("old\n", encoding="utf-8")
        (self.root / "requirements.txt").write_text("pybit==1\n", encoding="utf-8")
        (self.root / "config.yaml").write_text("dry_run: true\n", encoding="utf-8")
        (self.root / ".env").write_text(f"GITHUB_TOKEN={TOKEN}\n", encoding="utf-8")
        (self.root / "state").mkdir()
        (self.root / "state" / "update.json").write_text(
            json.dumps({"version": "1.0.0", "commit": OLD}) + "\n", encoding="utf-8")
        self.package = _zip({
            "VERSION": "1.1.0\n",
            "bot.py": "new\n",
            "requirements.txt": "pybit==2\n",
            "config.yaml": "dry_run: true\nnew_key: 1\n",
            "CHANGELOG.md": "# Changelog\n\n## 1.1.0\n\n- Faster entries\n\n## 1.0.0\n\n- Initial\n",
        }, prefix=f"bybit-sol-smc-bot-{NEW}/")
        self.fetches = []

    def tearDown(self):
        self._td.cleanup()

    def fetch(self, url):
        self.fetches.append(url)
        if url.endswith("/commits/main"):
            return _resp({"sha": NEW, "commit": {"message": "Release 1.1.0\n\nnotes",
                                                 "author": {"date": "2026-11-18T12:00:00Z"}}})
        if url.endswith("/VERSION"):
            return _resp("1.1.0\n")
        if url.endswith("/CHANGELOG.md"):
            return _resp("# Changelog\n\n## 1.1.0\n\n- Faster entries\n\n## 1.0.0\n\n- Initial\n")
        if "/compare/" in url:
            return _resp({"commits": [{"sha": NEW, "commit": {
                "message": "Release 1.1.0", "author": {"date": "2026-11-18T12:00:00Z"}}}]})
        if url.endswith("/zipball/main"):
            return _resp(self.package)
        raise AssertionError(url)

    def test_check_and_apply_with_fake_zipball(self):
        info = updater.check_for_updates(self.root, token=TOKEN, fetch=self.fetch)
        self.assertTrue(info["update_available"])
        self.assertEqual(info["remote_version"], "1.1.0")
        self.assertEqual(info["commits"][0]["message"], "Release 1.1.0")
        self.assertIn("Faster entries", info["changelog"])
        self.assertNotIn("Initial", info["changelog"])
        self.assertNotIn(TOKEN, json.dumps(info))
        pip_calls = []

        def pip(root):
            pip_calls.append(True)
            return True, "Successfully installed pybit-2"

        result = updater.perform_update(self.root, token=TOKEN, fetch=self.fetch, pip_runner=pip,
                                        status_fn=lambda: {"alive": False})
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "new\n")
        self.assertEqual(pip_calls, [True])
        self.assertIn("pip install succeeded", result.output)
        self.assertIn("new_key", result.output)
        self.assertTrue((self.root / "backups" / result.backup_id).is_dir())
        self.assertIn(TOKEN, (self.root / ".env").read_text(encoding="utf-8"))

    def test_refuses_while_position_open_and_does_not_write(self):
        def status():
            return {"alive": True, "mode": "LIVE", "position": {"side": "Buy", "size": "1"}, "pending_order": None}

        with self.assertRaises(updater.UpdateError) as caught:
            updater.perform_update(self.root, fetch=self.fetch, status_fn=status)
        self.assertIn("Stop the bot", str(caught.exception))
        self.assertEqual(self.fetches, [])
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "old\n")

    def test_refuses_if_a_position_appears_before_files_change(self):
        seen = {"n": 0}

        def status():
            seen["n"] += 1
            if seen["n"] == 1:
                return {"alive": False}
            return {"alive": True, "pending_order": {"id": "abc"}, "position": None}

        with self.assertRaises(updater.UpdateError):
            updater.perform_update(self.root, fetch=self.fetch, status_fn=status,
                                   pip_runner=lambda root: (True, ""))
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "old\n")
        self.assertFalse((self.root / "backups").exists())

    def test_running_but_flat_is_allowed(self):
        result = updater.perform_update(
            self.root, fetch=self.fetch, pip_runner=lambda root: (True, "ok"),
            status_fn=lambda: {"alive": True, "position": None, "pending_order": None})
        self.assertTrue(result.ok)
        self.assertIn("still running the old code", result.output)

    def test_baseline_then_commit_only_update(self):
        (self.root / "state" / "update.json").unlink()
        same = NEW

        def fetch(url, _same=same):
            if url.endswith("/commits/main"):
                return _resp({"sha": fetch.sha, "commit": {"message": "tip", "author": {"date": "2026-11-18T00:00:00Z"}}})
            if url.endswith("/VERSION"):
                return _resp("1.0.0\n")
            if url.endswith("/CHANGELOG.md"):
                return _resp("# Changelog\n\n## 1.0.0\n\n- Initial\n")
            if "/compare/" in url:
                return _resp({"commits": [{"sha": fetch.sha, "commit": {"message": "tip", "author": {"date": ""}}}]})
            if "commits?sha=main" in url:
                return _resp([{"sha": fetch.sha, "commit": {"message": "tip", "author": {"date": ""}}}])
            raise AssertionError(url)

        fetch.sha = same
        first = updater.check_for_updates(self.root, fetch=fetch)
        self.assertFalse(first["update_available"])
        self.assertEqual(json.loads((self.root / "state" / "update.json").read_text())["commit"], same)
        fetch.sha = OLD
        second = updater.check_for_updates(self.root, fetch=fetch)
        self.assertTrue(second["update_available"])
        self.assertEqual(second["remote_version"], "1.0.0")

    def test_pip_failure_is_reported_and_backup_remains(self):
        result = updater.perform_update(
            self.root, fetch=self.fetch, status_fn=lambda: {"alive": False},
            pip_runner=lambda root: (False, "error: Smart App Control blocked pip"))
        self.assertFalse(result.ok)
        self.assertIn("FAILED", result.output)
        self.assertIn("Smart App Control", result.output)
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "new\n")
        self.assertTrue(result.backup_id)
        rolled = updater.rollback(self.root, result.backup_id, status_fn=lambda: {"alive": False},
                                  pip_runner=lambda root: (True, "restored"))
        self.assertEqual((self.root / "bot.py").read_text(encoding="utf-8"), "old\n")
        self.assertIn("restored", rolled.output)


class HttpSafetyTests(unittest.TestCase):
    def test_token_header_stays_off_codeload(self):
        api = updater._request_headers("https://api.github.com/repos/x", TOKEN, "*/*")
        raw = updater._request_headers("https://raw.githubusercontent.com/x/VERSION", TOKEN, "*/*")
        code = updater._request_headers("https://codeload.github.com/x/legacy.zip?token=abc", TOKEN, "*/*")
        self.assertIn("Authorization", api)
        self.assertIn("Authorization", raw)
        self.assertNotIn("Authorization", code)
        self.assertEqual(api["User-Agent"], updater.USER_AGENT)

    def test_redirect_host_allowlist_and_redaction(self):
        def request(url, token, timeout, accept, max_bytes):
            host = urlsplit(url).hostname
            if host == "api.github.com":
                return 302, {"location": "https://evil.example/steal?token=" + TOKEN}, b""
            raise AssertionError(host)

        with self.assertRaises(updater.UpdateError) as caught:
            updater.http_get("https://api.github.com/repos/x/zipball/main", token=TOKEN, request=request)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("evil.example", str(caught.exception))

        def via_codeload(url, token, timeout, accept, max_bytes):
            host = urlsplit(url).hostname
            if host == "api.github.com":
                return 302, {"location": "https://codeload.github.com/o/r/legacy.zip/main?token=one-time"}, b""
            if host == "codeload.github.com":
                return 200, {}, b"PK\x03\x04payload"
            raise AssertionError(host)

        followed = updater.http_get("https://api.github.com/repos/o/r/zipball/main", token=TOKEN, request=via_codeload)
        self.assertTrue(followed.body.startswith(b"PK"))
        self.assertEqual(urlsplit(followed.url).hostname, "codeload.github.com")

        def missing(url, token, timeout, accept, max_bytes):
            return 404, {}, f"bad credentials {TOKEN}".encode()

        with self.assertRaises(updater.UpdateError) as caught:
            updater.http_get("https://api.github.com/repos/x", token=TOKEN, request=missing)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("***", str(caught.exception))

        def missing_public(url, token, timeout, accept, max_bytes):
            return 404, {}, b"Not Found"

        with self.assertRaises(updater.UpdateError) as caught:
            updater.http_get("https://api.github.com/repos/x", token=None, request=missing_public)
        self.assertIn("GITHUB_TOKEN", str(caught.exception))

    def test_redact_and_dotenv_token(self):
        text = f"Authorization: Bearer {TOKEN} GITHUB_TOKEN={TOKEN} https://x?token={TOKEN}"
        cleaned = updater.redact(text, TOKEN)
        self.assertNotIn(TOKEN, cleaned)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            (path / ".env").write_text(f"# comment\nGITHUB_TOKEN=\"{TOKEN}\"\n", encoding="utf-8")
            self.assertEqual(updater.github_token(path), TOKEN)

    def test_rejects_credential_url(self):
        with self.assertRaises(updater.UpdateError):
            updater.http_get("https://user:pass@api.github.com/repos/x", request=lambda *a: (200, {}, b""))


class GitignoreTests(unittest.TestCase):
    def test_user_files_are_ignored(self):
        ignored = subprocess.check_output(
            ["git", "check-ignore", ".env", ".venv/x", "logs/bot.log", "state/update.json",
             "backups/20260101/bot.py", "backtest_trades.csv", "config.yaml.bak", "notes.bak"],
            cwd=ROOT, text=True).split()
        for path in (".env", ".venv/x", "logs/bot.log", "state/update.json",
                     "backups/20260101/bot.py", "backtest_trades.csv", "config.yaml.bak", "notes.bak"):
            self.assertIn(path, ignored)
        example = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT)
        self.assertEqual(example.returncode, 1)


if __name__ == "__main__":
    unittest.main()
