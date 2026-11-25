"""relay_state.py のテスト。"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "relay_state", Path(__file__).parents[1] / "scripts" / "relay_state.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class TestStateDir(unittest.TestCase):
    def test_default_is_under_temp(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(module.tempfile, "gettempdir", return_value=tmp):
            self.assertEqual(module.state_dir(), Path(tmp) / f"agent-relay-{os.getuid()}" / "state")

    def test_env_override_wins(self):
        with patch.dict(os.environ, {"AGENT_RELAY_STATE_DIR": "/tmp/override"}):
            self.assertEqual(module.state_dir(), Path("/tmp/override"))

class TestDuplicate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AGENT_RELAY_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_round_trip(self):
        module.record_state("sess", "m1", "accepted", "claude")
        hit = module.check_duplicate("sess", "m1")
        self.assertEqual(hit["state"], "accepted")
        self.assertIsNone(module.check_duplicate("sess", "other"))
        self.assertIsNone(module.check_duplicate("other", "m1"))

    def test_a_then_b_keeps_a(self):
        module.record_state("sess", "A", "unknown", "claude")
        module.record_state("sess", "B", "accepted", "claude")
        self.assertEqual(module.read_state("sess", "A")["state"], "unknown")
        self.assertIsNotNone(module.check_duplicate("sess", "B"))

    def test_unknown_does_not_block_and_not_sent_does_not(self):
        module.record_state("a", "m2", "unknown", "opencode")
        self.assertIsNone(module.check_duplicate("a", "m2"))
        module.record_state("b", "m3", "not_sent", "codex")
        self.assertIsNone(module.check_duplicate("b", "m3"))

if __name__ == "__main__":
    unittest.main()
