"""opencode_send.py のテスト。"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "opencode_send", Path(__file__).parents[1] / "scripts" / "opencode_send.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class TestOpencodeSend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AGENT_RELAY_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(sys, "stdout", stdout), \
                patch.object(sys, "stderr", stderr):
            code = module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_stops_with_3_when_app_closed(self):
        argv = ["opencode_send.py", "sess-" + "001", "本文"]
        with (
            patch.object(module, "ensure_app_running", side_effect=SystemExit(3)),
            patch.object(sys, "argv", argv),
            self.assertRaises(SystemExit) as ctx,
        ):
            module.main()
        self.assertEqual(ctx.exception.code, 3)

    def test_sends_when_app_open(self):
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps(
            {"delivery": "steer", "data": {"id": "msg_1", "payload": {"text": "x"}}}))
        argv = ["opencode_send.py", "sess-" + "002", "依頼", "--sender", "Codex"]
        with patch.object(module, "ensure_app_running"), \
                patch.object(module.subprocess, "run", return_value=ok) as run:
            code, out, _ = self.run_main(argv)
        self.assertEqual(code, 0)
        run.assert_called_once()
        data = json.loads(out)
        self.assertEqual(data["delivery"], "steer")
        self.assertEqual(data["arrival"], "confirmed")
        self.assertEqual(data["message_object_id"], "msg_1")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "opencode")
        payload = json.loads(cmd[cmd.index("-d") + 1])
        self.assertIn("Codexから", payload["text"])
        self.assertIn("メッセージ ID:", payload["text"])

    def test_unparsable_response_is_accepted_unconfirmed(self):
        ok = subprocess.CompletedProcess([], 0, stdout="ok")
        with patch.object(module, "ensure_app_running"), \
                patch.object(module.subprocess, "run", return_value=ok):
            code, out, _ = self.run_main(["opencode_send.py", "sess-3", "依頼"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["arrival"], "unconfirmed")

    def test_session_not_found_is_not_sent(self):
        bad = subprocess.CompletedProcess(
            [], 1, stdout='{"_tag":"SessionNotFoundError"}', stderr="")
        with patch.object(module, "ensure_app_running"), \
                patch.object(module.subprocess, "run", return_value=bad):
            code, _, err = self.run_main(["opencode_send.py", "sess-x", "依頼"])
        self.assertEqual(code, 3)
        self.assertIn("セッションが見つかりません", err)

    def test_timeout_is_unknown_and_blocks_same_id(self):
        with patch.object(module, "ensure_app_running"), \
                patch.object(module.subprocess, "run",
                             side_effect=subprocess.TimeoutExpired("opencode", 1)):
            code, _, err = self.run_main(
                ["opencode_send.py", "sess-4", "依頼", "--message-id", "m1"])
        self.assertEqual(code, 1)
        self.assertIn("結果が不明", err)
        # 不明だけで永久に止めない
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps({"data": {"id": "m"}}))
        with patch.object(module, "ensure_app_running"), \
                patch.object(module.subprocess, "run", return_value=ok) as run:
            code, _, _ = self.run_main(
                ["opencode_send.py", "sess-4", "依頼", "--message-id", "m1"])
        self.assertEqual(code, 0)
        run.assert_called_once()

if __name__ == "__main__":
    unittest.main()
