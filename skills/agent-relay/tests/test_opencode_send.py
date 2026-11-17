"""opencode_send.py のテスト。"""
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "opencode_send", Path(__file__).parents[1] / "scripts" / "opencode_send.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class TestOpencodeSend(unittest.TestCase):
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
        ok = subprocess.CompletedProcess([], 0, stdout=json.dumps({"delivery": "steer"}))
        argv = ["opencode_send.py", "sess-" + "002", "依頼", "--sender", "Codex"]
        stdout = io.StringIO()
        with (
            patch.object(module, "ensure_app_running"),
            patch.object(module.subprocess, "run", return_value=ok) as run,
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdout", stdout),
        ):
            code = module.main()
        self.assertEqual(code, 0)
        run.assert_called_once()
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["delivery"], "steer")
        self.assertIn("message_id", data)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "opencode")
        d_idx = cmd.index("-d")
        payload = json.loads(cmd[d_idx + 1])
        self.assertIn("Codexから", payload["text"])
        self.assertIn("メッセージ ID:", payload["text"])

if __name__ == "__main__":
    unittest.main()