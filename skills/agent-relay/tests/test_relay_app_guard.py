"""relay_app_guard.py のテスト。"""
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "relay_app_guard", Path(__file__).parents[1] / "scripts" / "relay_app_guard.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class TestRelayAppGuard(unittest.TestCase):
    def test_closed_message_format(self):
        msg = module.app_closed_message("Test.app")
        self.assertIn("宛先アプリが閉じています", msg)
        self.assertIn("Test.app が起動していません", msg)
        self.assertIn("送り直してください", msg)

    def test_ensure_exits_when_not_running(self):
        binary = Path("/Applications/Example.app/Contents/MacOS/Example")
        with patch.object(module, "app_process_running", return_value=False):
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr), self.assertRaises(SystemExit) as ctx:
                module.ensure_app_running(binary, "Example.app")
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("Example.app", stderr.getvalue())

    def test_process_running_delegates_to_pgrep(self):
        binary = Path("/Applications/Example.app/Contents/MacOS/Example")
        with patch.object(
            module.subprocess,
            "run",
            side_effect=[
                type("R", (), {"returncode": 0, "stdout": "99\n"})(),
                type("R", (), {"returncode": 0, "stdout": f"{binary} --foo"})(),
            ],
        ) as run:
            self.assertTrue(module.app_process_running(binary))
        self.assertEqual(run.call_count, 2)

if __name__ == "__main__":
    unittest.main()