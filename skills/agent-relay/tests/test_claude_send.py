"""claude_send.py のテスト（実セッションには触れない）。"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "claude_send", Path(__file__).parents[1] / "scripts" / "claude_send.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"AGENT_RELAY_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), patch.object(sys, "stdin", io.StringIO(stdin)), \
                patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            code = module.main()
        return code, out.getvalue(), err.getvalue()

class TestFind(Base):
    def test_exits_3_when_no_session_and_no_record(self):
        with patch.object(module, "session_rows", return_value=[]), \
                patch.object(module, "record_lookup", return_value=[]), \
                patch.object(module, "record_title_scan", return_value=([], 0)):
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr), self.assertRaises(SystemExit) as ctx:
                module.find("missing-target")
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("記録も見つかりません", stderr.getvalue())

    def test_keeps_ambiguous_exit_when_multiple(self):
        prefix = "shared-"
        rows = [
            {"pid": 1, "name": "a", "sessionId": prefix + "aaa", "_live": True},
            {"pid": 2, "name": "b", "sessionId": prefix + "bbb", "_live": True},
        ]
        with patch.object(module, "session_rows", return_value=rows):
            with self.assertRaises(SystemExit) as ctx:
                module.find(prefix)
        self.assertIn("宛先を1つに絞れません", str(ctx.exception))

    def test_prefers_live_row_when_same_conversation_reappears(self):
        # 再開で同じ sessionId の json が複数残ることがある
        rows = [
            {"pid": 1, "sessionId": "abc", "_live": False},
            {"pid": 2, "sessionId": "abc", "_live": True},
        ]
        with patch.object(module, "session_rows", return_value=rows):
            row = module.find("abc")
        self.assertEqual(row["pid"], 2)

    def test_resolves_record_only_conversation(self):
        with patch.object(module, "session_rows", return_value=[]), \
                patch.object(module, "record_lookup", return_value=["abc-123"]), \
                patch.object(module, "record_path", return_value="/r/abc-123.jsonl"):
            row = module.find("abc")
        self.assertFalse(row["_live"])
        self.assertEqual(row["sessionId"], "abc-123")
        self.assertEqual(row["_record"], "/r/abc-123.jsonl")

    def test_resolves_unloaded_by_name(self):
        rows = [{"pid": 1, "name": "資料整理", "sessionId": "old-1",
                 "_live": False, "hostSessionId": "local_a"}]
        with patch.object(module, "session_rows", return_value=rows), \
                patch.object(module, "record_title_scan", return_value=([], 0)):
            row = module.find("資料整理")
        self.assertEqual(row["sessionId"], "old-1")
        self.assertFalse(row["_live"])

    def test_same_name_shows_candidates_and_stops(self):
        rows = [
            {"pid": 1, "name": "同名の会話", "sessionId": "a1", "_live": True},
            {"pid": 2, "name": "同名の会話", "sessionId": "b2", "_live": True},
        ]
        with patch.object(module, "session_rows", return_value=rows), \
                patch.object(module, "record_title_scan", return_value=([], 0)):
            with self.assertRaises(SystemExit) as ctx:
                module.find("同名の会話")
        message = str(ctx.exception)
        self.assertIn("絞れません", message)
        self.assertIn("a1", message)
        self.assertIn("b2", message)

    def test_old_session_id_resolves_to_current_live_row(self):
        old = {"pid": None, "name": "継続会話", "sessionId": "old-1",
               "_live": False, "hostSessionId": "local_x"}
        live = {"pid": 5, "name": "継続会話", "sessionId": "new-9",
                "_live": True, "hostSessionId": "local_x"}
        stderr = io.StringIO()
        with patch.object(module, "session_rows", return_value=[old, live]), \
                patch.object(sys, "stderr", stderr):
            row = module.find("old-1")
        self.assertEqual(row["sessionId"], "new-9")
        self.assertTrue(row["_live"])
        self.assertIn("同じ会話として起動中", stderr.getvalue())

    def test_finds_record_conversation_by_title(self):
        with patch.object(module, "session_rows", return_value=[]), \
                patch.object(module, "record_lookup", return_value=[]), \
                patch.object(module, "record_title_scan", return_value=(["rec-9"], 0)), \
                patch.object(module, "record_path", return_value="/r/rec-9.jsonl"):
            row = module.find("古い会話名")
        self.assertEqual(row["sessionId"], "rec-9")
        self.assertFalse(row["_live"])

    def test_record_title_reads_custom_title(self):
        path = os.path.join(self.tmp.name, "x.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": "a"}) + "\n")
            fh.write(json.dumps({"type": "custom-title", "customTitle": "資料整理の続き",
                                 "sessionId": "a"}) + "\n")
        self.assertEqual(module.record_title(path), "資料整理の続き")
        self.assertIsNone(module.record_title(os.path.join(self.tmp.name, "none.jsonl")))

    def test_same_name_live_and_unloaded_shows_candidates(self):
        # 同名でも別会話。起動中を勝手に選ばない
        rows = [
            {"pid": 1, "name": "同名", "sessionId": "live-1", "_live": True, "hostSessionId": "h1"},
            {"pid": 2, "name": "同名", "sessionId": "idle-2", "_live": False, "hostSessionId": "h2"},
        ]
        with patch.object(module, "session_rows", return_value=rows), \
                patch.object(module, "record_title_scan", return_value=([], 0)):
            with self.assertRaises(SystemExit) as ctx:
                module.find("同名")
        message = str(ctx.exception)
        self.assertIn("live-1", message)
        self.assertIn("idle-2", message)
        self.assertIn("起動中", message)
        self.assertIn("未ロード", message)

    def test_resolves_name_when_unique(self):
        rows = [{"pid": 1, "name": "ひとつだけ", "sessionId": "s1", "_live": True}]
        with patch.object(module, "session_rows", return_value=rows), \
                patch.object(module, "record_title_scan", return_value=([], 0)):
            row = module.find("ひとつだけ")
        self.assertEqual(row["sessionId"], "s1")

    def test_record_title_search_scans_beyond_recent_files(self):
        # 21 件目より古い記録にしか無い名前でも見つける
        projects = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(projects, "p"))
        for i in range(22):
            path = os.path.join(projects, "p", f"sid-{i:02d}.jsonl")
            title = "古い会話" if i == 0 else f"別の会話{i}"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")
            os.utime(path, (1000 + i, 1000 + i))
        with patch.object(module, "PROJECTS", projects):
            hits, unreadable = module.record_title_scan("古い会話")
        self.assertEqual(hits, ["sid-00"])
        self.assertEqual(unreadable, 0)

    def test_record_title_search_stops_on_multiple_matches(self):
        projects = os.path.join(self.tmp.name, "projects2")
        os.makedirs(os.path.join(projects, "p"))
        for i in range(25):
            path = os.path.join(projects, "p", f"x-{i:02d}.jsonl")
            title = "重複名" if i in (0, 21) else f"無関係{i}"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")
            os.utime(path, (1000 + i, 1000 + i))
        with patch.object(module, "PROJECTS", projects), \
                patch.object(module, "session_rows", return_value=[]), \
                patch.object(module, "record_lookup", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                module.find("重複名")
        message = str(ctx.exception)
        self.assertIn("x-00", message)
        self.assertIn("x-21", message)

    def test_unique_session_name_plus_record_only_same_name_stops(self):
        # セッション情報では一意でも、記録だけ残った同名の別会話があれば送らない
        rows = [{"pid": 1, "name": "整理", "sessionId": "live-1", "_live": True}]
        with patch.object(module, "session_rows", return_value=rows), \
                patch.object(module, "record_title_scan", return_value=(["old-2"], 0)):
            with self.assertRaises(SystemExit) as ctx:
                module.find("整理")
        message = str(ctx.exception)
        self.assertIn("live-1", message)
        self.assertIn("old-2", message)
        self.assertIn("記録のみ", message)

    def test_unreadable_record_prevents_unique_send(self):
        # 読めない記録があるときは一意と決めつけない
        rows = [{"pid": 1, "name": "整理", "sessionId": "live-1", "_live": True}]
        with patch.object(module, "session_rows", return_value=rows), \
                patch.object(module, "record_title_scan", return_value=([], 2)):
            with self.assertRaises(SystemExit) as ctx:
                module.find("整理")
        self.assertIn("読めず", str(ctx.exception))

    def test_list_shows_only_live_sessions(self):
        rows = [
            {"pid": 1, "sessionId": "a", "_live": True, "name": "n", "status": "idle"},
            {"pid": 2, "sessionId": "b", "_live": False, "name": "m", "status": "idle"},
        ]
        with patch.object(module, "live_sessions", return_value=[rows[0]]):
            stdout = io.StringIO()
            with patch.object(sys, "argv", ["claude_send.py", "list"]), \
                    patch.object(sys, "stdout", stdout):
                module.main()
        self.assertIn("n", stdout.getvalue())
        self.assertNotIn("m", stdout.getvalue())

class TestSendStates(Base):
    ROW = {"pid": 9, "name": "n", "sessionId": "sid", "messagingSocketPath": "/tmp/x.sock",
           "_live": True, "_record": "/r/sid.jsonl"}

    def test_not_loaded_with_app_open_reports_record_and_does_not_send(self):
        row = dict(self.ROW, _live=False, pid=None)
        with patch.object(module, "find", return_value=row), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "wake_conversation") as wake, \
                patch.object(module, "send") as send:
            code, _, err = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--no-wake"]
            )
        self.assertEqual(code, 3)
        self.assertIn("未ロード", err)
        wake.assert_not_called()
        send.assert_not_called()

    def test_wakes_unloaded_conversation_then_sends(self):
        row = dict(self.ROW, _live=False, pid=None, hostSessionId="local_x")
        live = dict(self.ROW, hostSessionId="local_x")
        with patch.object(module, "find", return_value=row), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "wake_conversation", return_value=(live, "opened")) as wake, \
                patch.object(module, "send") as send, \
                patch.object(module, "arrived_in_record", return_value=False):
            code, out, _ = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--message-id", "wake1"]
            )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["wake"], "opened")
        self.assertEqual(data["arrival"], "unconfirmed")
        wake.assert_called_once()
        self.assertEqual(send.call_args.args[0], "/tmp/x.sock")
        self.assertIn("メッセージ ID: wake1", send.call_args.args[1])

    def test_wake_failure_reports_and_does_not_send(self):
        row = dict(self.ROW, _live=False, pid=None)
        for reason in ("no_mapping", "timeout", "different_session", "open_failed"):
            with self.subTest(reason=reason), \
                    patch.object(module, "find", return_value=row), \
                    patch.object(module, "app_process_running", return_value=True), \
                    patch.object(module, "wake_conversation", return_value=(None, reason)), \
                    patch.object(module, "send") as send:
                code, _, err = self.run_main(["claude_send.py", "send", "sid", "本文"])
                self.assertEqual(code, 3)
                self.assertIn(module.WAKE_FAIL_MSG[reason], err)
                send.assert_not_called()

    def test_not_loaded_with_app_closed_asks_to_open_app(self):
        row = dict(self.ROW, _live=False, pid=None)
        with patch.object(module, "find", return_value=row), \
                patch.object(module, "app_process_running", return_value=False), \
                patch.object(module, "send") as send:
            code, _, err = self.run_main(["claude_send.py", "send", "sid", "本文"])
        self.assertEqual(code, 3)
        self.assertIn("宛先アプリが閉じています", err)
        send.assert_not_called()

    def test_live_send_reports_accepted_and_unconfirmed(self):
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send") as send, \
                patch.object(module, "arrived_in_record", return_value=False):
            code, out, _ = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--sender", "Codex"]
            )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["status"], "accepted")
        self.assertEqual(data["arrival"], "unconfirmed")
        self.assertEqual(data["session_id"], "sid")
        sent_text = send.call_args.args[1]
        self.assertTrue(sent_text.startswith("Codexから\nメッセージ ID: "))
        self.assertIn("本文", sent_text)

    def test_live_send_confirms_arrival_from_record(self):
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send"), \
                patch.object(module, "arrived_in_record", return_value=True):
            code, out, _ = self.run_main(["claude_send.py", "send", "sid", "本文"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["arrival"], "confirmed")

    def test_connect_failure_is_not_sent_and_retry_allowed(self):
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send", side_effect=module.NotSent("接続不可")):
            code, _, err = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--message-id", "m1"]
            )
        self.assertEqual(code, 3)
        self.assertIn("未送信", err)
        # 未送信は再送してよい
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send") as send, \
                patch.object(module, "arrived_in_record", return_value=False):
            code, _, _ = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--message-id", "m1"]
            )
        self.assertEqual(code, 0)
        send.assert_called_once()

    def test_unknown_result_allows_explicit_retry(self):
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send", side_effect=module.UnknownResult("切断")):
            code, _, err = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--message-id", "m2"]
            )
        self.assertEqual(code, 1)
        self.assertIn("結果が不明", err)
        # 不明だけで永久に止めない。記録を確認したうえでの再試行は許す
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send") as send, \
                patch.object(module, "arrived_in_record", return_value=False):
            code, _, _ = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--message-id", "m2"]
            )
        self.assertEqual(code, 0)
        send.assert_called_once()

    def test_accepted_blocks_resend_with_same_id(self):
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send"), \
                patch.object(module, "arrived_in_record", return_value=False):
            self.run_main(["claude_send.py", "send", "sid", "本文", "--message-id", "m3"])
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send") as send:
            code, _, _ = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--message-id", "m3"]
            )
        self.assertEqual(code, 3)
        send.assert_not_called()

    def test_resend_flag_allows_explicit_retry(self):
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send"), \
                patch.object(module, "arrived_in_record", return_value=False):
            self.run_main(["claude_send.py", "send", "sid", "本文", "--message-id", "m4"])
        with patch.object(module, "find", return_value=self.ROW), \
                patch.object(module, "app_process_running", return_value=True), \
                patch.object(module, "send") as send, \
                patch.object(module, "arrived_in_record", return_value=False):
            code, _, _ = self.run_main(
                ["claude_send.py", "send", "sid", "本文", "--message-id", "m4", "--resend"]
            )
        self.assertEqual(code, 0)
        send.assert_called_once()

class TestWake(Base):
    def test_local_session_id_uses_newest_matching_log(self):
        logdir = Path(self.tmp.name)
        old = logdir / "main1.log"
        old.write_text("Mapping internal session local_aaa to CLI session cafe\n", encoding="utf-8")
        os.utime(old, (1, 1))
        (logdir / "main.log").write_text(
            "x\nMapping internal session local_bbb to CLI session cafe\n", encoding="utf-8")
        with patch.object(module, "CLAUDE_LOG_DIR", str(logdir)):
            self.assertEqual(module.local_session_id("cafe"), "local_bbb")

    def test_local_session_id_returns_none_without_mapping(self):
        with patch.object(module, "CLAUDE_LOG_DIR", self.tmp.name):
            self.assertIsNone(module.local_session_id("cid"))

    def test_wake_opens_and_returns_live_row(self):
        live = {"sessionId": "cid", "hostSessionId": "local_a", "_live": True}
        with patch.object(module.shutil, "which", return_value="/usr/bin/open"), \
                patch.object(module.subprocess, "run") as run, \
                patch.object(module, "session_rows", return_value=[live]), \
                patch.object(module.time, "monotonic", return_value=0):
            row, reason = module.wake_conversation("cid", "local_a", 10)
        self.assertEqual(reason, "opened")
        self.assertIs(row, live)
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/open", "-g", "claude://code/continue?session=local_a"],
        )

    def test_wake_refuses_when_other_session_starts(self):
        other = {"sessionId": "cid2", "hostSessionId": "local_a", "_live": True}
        with patch.object(module.shutil, "which", return_value="/usr/bin/open"), \
                patch.object(module.subprocess, "run"), \
                patch.object(module, "session_rows", return_value=[other]), \
                patch.object(module.time, "monotonic", return_value=0):
            row, reason = module.wake_conversation("cid", "local_a", 10)
        self.assertIsNone(row)
        self.assertEqual(reason, "different_session")

    def test_wake_times_out_without_socket(self):
        with patch.object(module.shutil, "which", return_value="/usr/bin/open"), \
                patch.object(module.subprocess, "run"), \
                patch.object(module, "session_rows", return_value=[]), \
                patch.object(module.time, "sleep"), \
                patch.object(module.time, "monotonic", side_effect=[0, 0, 11]):
            row, reason = module.wake_conversation("cid", "local_a", 10)
        self.assertIsNone(row)
        self.assertEqual(reason, "timeout")

    def test_wake_without_internal_id_does_not_open(self):
        with patch.object(module.subprocess, "run") as run:
            row, reason = module.wake_conversation("cid", None, 1)
        self.assertIsNone(row)
        self.assertEqual(reason, "no_mapping")
        run.assert_not_called()

class TestSendSocket(Base):
    def test_missing_socket_is_not_sent(self):
        with self.assertRaises(module.NotSent):
            module.send(str(Path(self.tmp.name) / "nope.sock"), "本文")

    def test_writes_one_json_line(self):
        import socket as s
        path = str(Path(self.tmp.name) / "s.sock")
        server = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        try:
            module.send(path, "本文")
            conn, _ = server.accept()
            with conn:
                data = conn.recv(65536)
        finally:
            server.close()
        frame = json.loads(data.decode().strip())
        self.assertEqual(frame["type"], "user")
        self.assertEqual(frame["message"], {"role": "user", "content": "本文"})

class TestRecordCheck(Base):
    def test_arrived_in_record_finds_message_id_at_tail(self):
        path = Path(self.tmp.name) / "rec.jsonl"
        user = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "メッセージ ID: abc"}]}}
        assistant = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "メッセージ ID: abc"}]}}
        partial = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "メッセージ ID: abc-def"}]}}
        path.write_text("\n".join(json.dumps(x) for x in (user, assistant, partial)) + "\n",
                        encoding="utf-8")
        self.assertTrue(module.arrived_in_record(str(path), "abc"))
        self.assertFalse(module.arrived_in_record(str(path), "zzz"))
        self.assertFalse(module.arrived_in_record(str(path), "abc-def-extra"))

if __name__ == "__main__":
    unittest.main()
