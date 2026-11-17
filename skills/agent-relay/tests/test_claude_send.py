"""claude_send.py のテスト（実セッションには触れない）。"""
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "claude_send", Path(__file__).parents[1] / "scripts" / "claude_send.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class TestClaudeSend(unittest.TestCase):
    def test_find_exits_3_when_no_session(self):
        with patch.object(module, "sessions", return_value=[]):
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr), self.assertRaises(SystemExit) as ctx:
                module.find("missing-target")
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("セッションが見つかりません", stderr.getvalue())

    def test_find_keeps_ambiguous_exit_when_multiple(self):
        prefix = "shared-"
        rows = [
            {"pid": 1, "name": "a", "sessionId": prefix + "aaa"},
            {"pid": 2, "name": "b", "sessionId": prefix + "bbb"},
        ]
        with patch.object(module, "sessions", return_value=rows):
            with self.assertRaises(SystemExit) as ctx:
                module.find(prefix)
        self.assertIn("宛先を1つに絞れません", str(ctx.exception))

    def test_send_invokes_socket_when_session_resolved(self):
        row = {"messagingSocketPath": "/tmp/fake.sock", "pid": 1, "name": "n", "sessionId": "s"}
        with patch.object(module, "find", return_value=row), patch.object(
            module, "send"
        ) as send_mock:
            module.find("target")
            module.send(row["messagingSocketPath"], "本文")
        send_mock.assert_called_once_with("/tmp/fake.sock", "本文")

if __name__ == "__main__":
    unittest.main()