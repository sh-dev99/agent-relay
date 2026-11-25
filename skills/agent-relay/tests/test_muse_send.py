"""muse_send.py のテスト。"""
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "muse_send", Path(__file__).parents[1] / "scripts" / "muse_send.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class TestMuseSend(unittest.TestCase):
    def test_stops_with_3_when_app_closed(self):
        argv = [
            "muse_send.py",
            "本文",
            "--sender",
            "Codex",
            "--subject",
            "件名",
            "--reply",
            "返信方法",
        ]
        with (
            patch.object(module, "ensure_app_running", side_effect=SystemExit(3)),
            patch.object(sys, "argv", argv),
            self.assertRaises(SystemExit) as ctx,
        ):
            module.main()
        self.assertEqual(ctx.exception.code, 3)

    def test_places_file_when_app_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp)
            argv = [
                "muse_send.py",
                "依頼本文",
                "--sender",
                "Claude",
                "--subject",
                "テスト",
                "--reply",
                "ファイルに書く",
            ]
            stdout = io.StringIO()
            with (
                patch.object(module, "ensure_app_running"),
                patch.object(module, "inbox_dir", return_value=inbox),
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdout", stdout),
            ):
                code = module.main()
            self.assertEqual(code, 0)
            name = stdout.getvalue().strip()
            path = inbox / name
            self.assertTrue(path.is_file())
            text = path.read_text(encoding="utf-8")
            self.assertIn("送り主: Claude", text)
            self.assertIn("件名: テスト", text)
            self.assertIn("依頼本文", text)
            self.assertIn("返信先:", text)
            self.assertIn("ファイルに書く", text)

    def test_missing_inbox_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no-inbox"
            argv = [
                "muse_send.py",
                "x",
                "--sender",
                "a",
                "--subject",
                "b",
                "--reply",
                "c",
            ]
            with (
                patch.object(module, "ensure_app_running"),
                patch.object(module, "inbox_dir", return_value=missing),
                patch.object(sys, "argv", argv),
            ):
                code = module.main()
            self.assertEqual(code, 1)

if __name__ == "__main__":
    unittest.main()