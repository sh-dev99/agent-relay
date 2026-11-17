import importlib.util
import io
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
                "keychain",
                side_effect=["http://127.0.0.1/webhook", "test-key"],
            ),
            patch("urllib.request.urlopen", return_value=unittest.mock.MagicMock(
                __enter__=lambda s: fake_res, __exit__=lambda *a: None
            )),
        ):
            module.send("テスト", "sender", log_path=log_path)
        self.assertTrue(inbox.exists())
        self.assertIn("テスト", inbox.read_text(encoding="utf-8"))

    def test_send_skips_inbox_when_unreachable(self):
        inbox = Path(tempfile.mkdtemp()) / "grokbot.md"
        with (
            patch.object(module, "grok_bot_process_running", return_value=False),
            patch.object(module, "INBOX", inbox),
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
                "keychain",
                side_effect=["http://127.0.0.1/webhook", "test-key"],
            ),
            patch("urllib.request.urlopen", return_value=unittest.mock.MagicMock(
                __enter__=lambda s: fake_res, __exit__=lambda *a: None
            )),
        ):
            module.send("テスト", "sender", force=True)
        self.assertTrue(inbox.exists())

if __name__ == "__main__":
    import unittest.mock
    unittest.main()