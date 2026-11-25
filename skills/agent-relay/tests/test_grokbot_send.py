import importlib.util
import json
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
import unittest.mock
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "grokbot_send", Path(__file__).parents[1] / "scripts" / "grokbot_send.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SAMPLE_LOG_CONNECTED = "\n".join([
    "[sand-local-exec-daemon] started (pid 1); serving local exec over the gateway",
    "[user-computer-watch] 2026-11-17T03:00:00.000Z connected after 100 ms out (attempts 1)",
])

SAMPLE_LOG_DISCONNECTED = "\n".join([
    "[user-computer-watch] 2026-11-17T03:00:00.000Z connected after 100 ms out (attempts 1)",
    "[user-computer-watch] 2026-11-17T03:10:00.000Z closed after 500 ms connected",
])

class TestLocalExecLog(unittest.TestCase):
    def test_last_event_connected(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(SAMPLE_LOG_CONNECTED)
            path = Path(f.name)
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertTrue(module.last_local_exec_connection_state(path))

    def test_last_event_disconnected(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(SAMPLE_LOG_DISCONNECTED)
            path = Path(f.name)
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertFalse(module.last_local_exec_connection_state(path))

    def test_unreadable_log_returns_none(self):
        missing = Path("/tmp/grokbot-nonexistent-log-for-test")
        self.assertIsNone(module.last_local_exec_connection_state(missing))

class TestEnsureReachable(unittest.TestCase):
    def test_stops_when_process_missing(self):
        with patch.object(module, "grok_bot_process_running", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                module.ensure_grok_bot_reachable()
        self.assertEqual(ctx.exception.code, 3)

    def test_stops_when_log_shows_disconnected(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(SAMPLE_LOG_DISCONNECTED)
            path = Path(f.name)
        self.addCleanup(path.unlink, missing_ok=True)
        with (
            patch.object(module, "grok_bot_process_running", return_value=True),
            self.assertRaises(SystemExit) as ctx,
        ):
            module.ensure_grok_bot_reachable(log_path=path)
        self.assertEqual(ctx.exception.code, 3)

    def test_warns_and_continues_when_log_unreadable(self):
        stderr = io.StringIO()
        missing = Path("/tmp/grokbot-missing-log-ensure-test")
        with (
            patch.object(module, "grok_bot_process_running", return_value=True),
            patch.object(sys, "stderr", stderr),
        ):
            module.ensure_grok_bot_reachable(log_path=missing, warn=stderr)
        self.assertIn("読めない", stderr.getvalue())

class TestSend(unittest.TestCase):
    def _open(self, fake_res):
        def urlopen(req, timeout=30):
            self.last_req = req
            return unittest.mock.MagicMock(
                __enter__=lambda s: fake_res, __exit__=lambda *a: None
            )
        return urlopen

    def test_send_when_ready(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(SAMPLE_LOG_CONNECTED)
            log_path = Path(f.name)
        self.addCleanup(log_path.unlink, missing_ok=True)
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        inbox = Path(tmp) / "grokbot.md"
        fake_res = type("R", (), {"status": 200, "read": lambda self: b'{"success":true}'})()
        with (
            patch.object(module, "grok_bot_process_running", return_value=True),
            patch.object(module, "INBOX", inbox),
            patch.object(module, "LOCAL_EXEC_LOG", log_path),
            patch.object(
                module,
                "read_keychain",
                side_effect=["http://127.0.0.1/webhook", "test-key"],
            ),
            patch("urllib.request.urlopen", side_effect=self._open(fake_res)),
        ):
            module.send("テスト", "sender", log_path=log_path)
        self.assertTrue(inbox.exists())
        self.assertIn("テスト", inbox.read_text(encoding="utf-8"))
        payload = json.loads(self.last_req.data.decode("utf-8"))
        self.assertEqual(payload["from"], "sender")
        self.assertEqual(payload["request"], "テスト")
        self.assertEqual(payload["task"], module.WEBHOOK_TASK)
        self.assertEqual(self.last_req.full_url, "http://127.0.0.1/webhook")

    def test_send_skips_inbox_when_unreachable(self):
        inbox = Path(tempfile.mkdtemp()) / "grokbot.md"
        with (
            patch.object(module, "grok_bot_process_running", return_value=False),
            patch.object(module, "INBOX", inbox),
            patch.object(
                module,
                "read_keychain",
                side_effect=["http://127.0.0.1/webhook", "test-key"],
            ),
            self.assertRaises(SystemExit) as ctx,
        ):
            module.send("テスト", "sender")
        self.assertEqual(ctx.exception.code, 3)
        self.assertFalse(inbox.exists())

    def test_force_skips_check(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        inbox = Path(tmp) / "grokbot.md"
        fake_res = type("R", (), {"status": 200, "read": lambda self: b"{}"})()
        with (
            patch.object(module, "grok_bot_process_running", return_value=False),
            patch.object(module, "INBOX", inbox),
            patch.object(
                module,
                "read_keychain",
                side_effect=["http://127.0.0.1/webhook", "test-key"],
            ),
            patch("urllib.request.urlopen", side_effect=self._open(fake_res)),
        ):
            module.send("テスト", "sender", force=True)
        self.assertTrue(inbox.exists())
        payload = json.loads(self.last_req.data.decode("utf-8"))
        self.assertEqual(payload["request"], "テスト")

    def test_env_auth_skips_inbox_and_app_check(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        inbox = Path(tmp) / "grokbot.md"
        fake_res = type("R", (), {"status": 200, "read": lambda self: b'{"success":true}'})()
        env = {
            "GROKBOT_WEBHOOK_URL": "http://127.0.0.1/env-webhook",
            "GROKBOT_WEBHOOK_KEY": "env-key",
        }
        with (
            patch.object(module, "read_keychain", return_value=None),
            patch.object(module, "grok_bot_process_running", side_effect=AssertionError("呼ばれない")),
            patch.object(module, "ensure_grok_bot_reachable", side_effect=AssertionError("呼ばれない")),
            patch.object(module, "INBOX", inbox),
            patch.dict(os.environ, env, clear=False),
            patch("urllib.request.urlopen", side_effect=self._open(fake_res)),
        ):
            module.send("クラウドからの依頼", "claude-cloud")
        self.assertFalse(inbox.exists())
        payload = json.loads(self.last_req.data.decode("utf-8"))
        self.assertEqual(payload["from"], "claude-cloud")
        self.assertEqual(payload["request"], "クラウドからの依頼")
        self.assertEqual(payload["task"], module.WEBHOOK_TASK)
        self.assertEqual(self.last_req.full_url, "http://127.0.0.1/env-webhook")
        self.assertEqual(self.last_req.headers["Authorization"], "Bearer env-key")

    def test_keychain_preferred_over_env(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        inbox = Path(tmp) / "grokbot.md"
        fake_res = type("R", (), {"status": 200, "read": lambda self: b"{}"})()

        def read(service):
            return {
                "grokbot-webhook-url": "http://127.0.0.1/keychain",
                "grokbot-webhook-key": "kc-key",
            }[service]

        env = {
            "GROKBOT_WEBHOOK_URL": "http://127.0.0.1/env-webhook",
            "GROKBOT_WEBHOOK_KEY": "env-key",
        }
        with (
            patch.object(module, "read_keychain", side_effect=read),
            patch.object(module, "grok_bot_process_running", return_value=True),
            patch.object(module, "last_local_exec_connection_state", return_value=True),
            patch.object(module, "INBOX", inbox),
            patch.dict(os.environ, env, clear=False),
            patch("urllib.request.urlopen", side_effect=self._open(fake_res)),
        ):
            module.send("本文", "sender", force=True)
        self.assertTrue(inbox.exists())
        self.assertEqual(self.last_req.full_url, "http://127.0.0.1/keychain")

    def test_security_command_missing_falls_back_to_env(self):
        # security コマンド自体が無い環境（クラウドの Linux）を模擬
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        inbox = Path(tmp) / "grokbot.md"
        fake_res = type("R", (), {"status": 200, "read": lambda self: b"{}"})()
        env = {
            "GROKBOT_WEBHOOK_URL": "http://127.0.0.1/env-webhook",
            "GROKBOT_WEBHOOK_KEY": "env-key",
        }
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("security")),
            patch.object(module, "ensure_grok_bot_reachable", side_effect=AssertionError("呼ばれない")),
            patch.object(module, "INBOX", inbox),
            patch.dict(os.environ, env, clear=False),
            patch("urllib.request.urlopen", side_effect=self._open(fake_res)),
        ):
            module.send("本文", "claude-cloud")
        self.assertFalse(inbox.exists())
        self.assertEqual(self.last_req.full_url, "http://127.0.0.1/env-webhook")

    def test_missing_auth_exits(self):
        with (
            patch.object(module, "read_keychain", return_value=None),
            patch.dict(os.environ, {"GROKBOT_WEBHOOK_URL": "", "GROKBOT_WEBHOOK_KEY": ""}, clear=False),
            self.assertRaises(SystemExit) as ctx,
        ):
            module.send("本文", "sender")
        self.assertIn("GROKBOT_WEBHOOK_URL", str(ctx.exception))

if __name__ == "__main__":
    import unittest.mock
    unittest.main()