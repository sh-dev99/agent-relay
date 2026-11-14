"""agy_send.py のテスト。一時 DB とモックだけを使い、実セッションには触れない。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agy_send  # noqa: E402

COLUMNS = (
    "conversation_id",
    "title",
    "status",
    "step_count",
    "last_modified_time",
    "source",
    "app_data_dir",
    "preview",
    "raw_summary",
)
PREVIEW_MARK = "秘匿プレビュー本文"

APP_ROW = {
    "conversation_id": "conv-app-0001",
    "title": "アプリ会話1",
    "status": "IDLE",
    "step_count": 3,
    "last_modified_time": 100,
    "source": "CORTEX_TRAJECTORY_SOURCE_APP",
    "app_data_dir": "antigravity",
}
CLI_ROW = {
    "conversation_id": "conv-cli-0001",
    "title": "CLI会話1",
    "status": "IDLE",
    "step_count": 1,
    "last_modified_time": 90,
    "source": agy_send.CLI_SOURCE,
    "app_data_dir": "antigravity-cli",
}

def make_db(path: Path, rows: list[dict]) -> None:
    """テスト用の会話DBを作る。"""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT, status TEXT, step_count INTEGER,"
            "last_modified_time INTEGER, source TEXT, app_data_dir TEXT,"
            "preview TEXT, raw_summary TEXT)"
        )
        for row in rows:
            values = dict({c: None for c in COLUMNS})
            values.update(row)
            values["preview"] = PREVIEW_MARK
            values["raw_summary"] = PREVIEW_MARK
            con.execute(
                f"INSERT INTO conversation_summaries ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                [values[c] for c in COLUMNS],
            )
        con.commit()
    finally:
        con.close()

def run_main(argv: list[str]) -> tuple[int, str]:
    """main を実行して (終了コード, 標準出力) を返す。SystemExit はここで捕まえる。"""
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            agy_send.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                buf.write(e.code)
    return code, buf.getvalue()

def completed(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

class AgySendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-relay-agy-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app_db = self.tmp / "app.db"
        self.cli_db = self.tmp / "cli.db"
        self.daemon_dir = self.tmp / "daemon"
        for name, value in (
            ("APP_DB", self.app_db),
            ("CLI_DB", self.cli_db),
            ("DAEMON_DIR", self.daemon_dir),
        ):
            patcher = mock.patch.object(agy_send, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # 実行ファイルの探索と接続先はテスト用に固定する
        patcher = mock.patch.object(agy_send, "find_bin", side_effect=lambda env, cmd, path: cmd)
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patcher = mock.patch.dict(
            os.environ,
            {"ANTIGRAVITY_LS_ADDRESS": "localhost:12345", "ANTIGRAVITY_CSRF_TOKEN": "test-csrf"},
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def test_falls_back_to_temp_copy_including_uncheckpointed_wal(self) -> None:
        make_db(self.app_db, [APP_ROW])
        writer = sqlite3.connect(self.app_db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                f"INSERT INTO conversation_summaries ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                [
                    "conv-app-wal",
                    "WAL未反映タイトル",
                    "IDLE",
                    5,
                    200,
                    "CORTEX_TRAJECTORY_SOURCE_APP",
                    "antigravity",
                    PREVIEW_MARK,
                    PREVIEW_MARK,
                ],
            )
            writer.commit()
            wal_file = Path(f"{self.app_db}-wal")
            self.assertTrue(wal_file.is_file() and wal_file.stat().st_size > 0)

            real_connect = sqlite3.connect
            seen: list[tuple[str, bool]] = []

            def fake_connect(database, **kwargs):
                seen.append((str(database), bool(kwargs.get("uri"))))
                if "mode=ro" in str(database):
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(database, **kwargs)

            with mock.patch.object(agy_send.sqlite3, "connect", side_effect=fake_connect):
                rows = agy_send.read_summaries(self.app_db)
        finally:
            writer.close()

        self.assertEqual([r["conversation_id"] for r in rows], ["conv-app-wal", "conv-app-0001"])
        self.assertEqual(rows[0]["title"], "WAL未反映タイトル")
        self.assertNotIn(PREVIEW_MARK, json.dumps(rows, ensure_ascii=False))
        self.assertIn("mode=ro", seen[0][0])
        self.assertNotIn("immutable=1", seen[1][0])
        self.assertFalse(seen[1][1])
        self.assertFalse(Path(seen[1][0]).parent.exists())

    def test_read_summaries_exits_when_temp_copy_also_fails(self) -> None:
        make_db(self.app_db, [APP_ROW])
        with mock.patch.object(
            agy_send.sqlite3, "connect", side_effect=sqlite3.OperationalError("blocked")
        ), mock.patch.object(agy_send.tempfile, "TemporaryDirectory", side_effect=PermissionError("ro")):
            with self.assertRaisesRegex(SystemExit, "会話一覧を読めません"):
                agy_send.read_summaries(self.app_db)

    def test_temp_copy_retries_on_concurrent_change_and_succeeds(self) -> None:
        make_db(self.app_db, [APP_ROW])
        writer = sqlite3.connect(self.app_db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.commit()

            real_connect = sqlite3.connect
            real_copyfile = agy_send.shutil.copyfile
            triggered = False

            def racy_copyfile(src, dst, *args, **kwargs):
                nonlocal triggered
                result = real_copyfile(src, dst, *args, **kwargs)
                if not triggered and Path(src) == self.app_db:
                    triggered = True
                    writer.execute(
                        f"INSERT INTO conversation_summaries ({', '.join(COLUMNS)}) "
                        f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                        [
                            "conv-app-retry",
                            "再試行後タイトル",
                            "IDLE",
                            6,
                            250,
                            "CORTEX_TRAJECTORY_SOURCE_APP",
                            "antigravity",
                            PREVIEW_MARK,
                            PREVIEW_MARK,
                        ],
                    )
                    writer.commit()
                return result

            def fake_connect(database, **kwargs):
                if "mode=ro" in str(database):
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(database, **kwargs)

            with mock.patch.object(agy_send.sqlite3, "connect", side_effect=fake_connect), mock.patch.object(
                agy_send.shutil, "copyfile", side_effect=racy_copyfile
            ), mock.patch.object(agy_send.time, "sleep") as sleep:
                rows = agy_send.read_summaries(self.app_db)
        finally:
            writer.close()

        self.assertTrue(triggered)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual([r["conversation_id"] for r in rows], ["conv-app-retry", "conv-app-0001"])

    def test_temp_copy_fails_closed_when_db_keeps_mutating(self) -> None:
        make_db(self.app_db, [APP_ROW])
        seq = iter(range(100))
        with mock.patch.object(
            agy_send.sqlite3, "connect", side_effect=sqlite3.OperationalError("blocked")
        ), mock.patch.object(
            agy_send, "_db_sigs", side_effect=lambda _p: ((next(seq),),)
        ), mock.patch.object(agy_send.time, "sleep") as sleep:
            with self.assertRaisesRegex(SystemExit, "一貫した複製を取得できません"):
                agy_send.read_summaries(self.app_db)
        self.assertEqual(sleep.call_count, agy_send.SNAPSHOT_RETRIES - 1)

    def test_temp_copy_fails_closed_on_quick_check_failure(self) -> None:
        make_db(self.app_db, [APP_ROW])
        real_connect = sqlite3.connect

        class BadQuickCheckConn:
            def __init__(self, inner):
                self._inner = inner

            def __setattr__(self, name, value):
                if name == "_inner":
                    super().__setattr__(name, value)
                else:
                    setattr(self._inner, name, value)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def execute(self, sql, *args, **kwargs):
                if "PRAGMA quick_check" in sql:
                    class FakeCursor:
                        def fetchone(self):
                            return ("database disk image is malformed",)

                    return FakeCursor()
                return self._inner.execute(sql, *args, **kwargs)

        def fake_connect(database, **kwargs):
            if "mode=ro" in str(database):
                raise sqlite3.OperationalError("blocked")
            return BadQuickCheckConn(real_connect(database, **kwargs))

        with mock.patch.object(agy_send.sqlite3, "connect", side_effect=fake_connect):
            with self.assertRaisesRegex(SystemExit, "quick_check 失敗"):
                agy_send.read_summaries(self.app_db)

    def test_missing_db_returns_empty(self) -> None:
        self.assertEqual(agy_send.read_summaries(self.tmp / "none.db"), [])

    def test_preview_and_raw_summary_are_not_read(self) -> None:
        make_db(self.app_db, [APP_ROW])
        rows = agy_send.read_summaries(self.app_db)
        expected = {
            "conversation_id",
            "title",
            "status",
            "step_count",
            "last_modified_time",
            "source",
            "app_data_dir",
        }
        self.assertEqual(set(rows[0]), expected)
        self.assertNotIn(PREVIEW_MARK, json.dumps(rows, ensure_ascii=False))

    def test_app_and_cli_are_separated(self) -> None:
        make_db(
            self.app_db,
            [
                APP_ROW,
                CLI_ROW,  # アプリ側 DB に混ざった CLI 会話
                dict(APP_ROW, conversation_id="conv-app-0002", app_data_dir="antigravity-cli"),
            ],
        )
        make_db(
            self.cli_db,
            [
                dict(CLI_ROW, title="CLI会話1（CLI側DB）", last_modified_time=95),
                dict(CLI_ROW, conversation_id="conv-cli-0002", title="CLI会話2"),
            ],
        )

        self.assertEqual([r["conversation_id"] for r in agy_send.app_conversations()], ["conv-app-0001"])
        cli = agy_send.cli_conversations()
        self.assertEqual(
            [r["conversation_id"] for r in cli],
            ["conv-cli-0001", "conv-cli-0002", "conv-app-0002"],
        )
        # 重複は CLI 側 DB の行を優先する
        self.assertEqual(cli[0]["title"], "CLI会話1（CLI側DB）")

    def test_send_to_cli_conversation_without_cli_flag(self) -> None:
        make_db(self.app_db, [CLI_ROW])
        with self.assertRaises(SystemExit) as ctx:
            agy_send.main(["send", "conv-cli-0001", "本文"])
        self.assertIn("--cli", str(ctx.exception))
        self.assertIn("agentapi", str(ctx.exception))

    def test_send_to_app_conversation_with_cli_flag(self) -> None:
        make_db(self.app_db, [APP_ROW])
        with self.assertRaises(SystemExit) as ctx:
            agy_send.main(["send", "conv-app-0001", "本文", "--cli"])
        self.assertIn("外して送信してください", str(ctx.exception))

    def test_resolve_exact_title_and_prefix(self) -> None:
        rows = [
            dict(APP_ROW, conversation_id="conv-app-0001", title="設計相談"),
            dict(APP_ROW, conversation_id="conv-app-0100", title="実装相談"),
        ]
        self.assertEqual(agy_send.pick(rows, "conv-app-0100")["conversation_id"], "conv-app-0100")
        self.assertEqual(agy_send.pick(rows, "設計相談")["conversation_id"], "conv-app-0001")
        self.assertEqual(agy_send.pick(rows, "conv-app-000")["conversation_id"], "conv-app-0001")

    def test_resolve_duplicate_and_missing(self) -> None:
        rows = [
            dict(APP_ROW, conversation_id="conv-app-0001", title="同名"),
            dict(APP_ROW, conversation_id="conv-app-0002", title="同名"),
        ]
        with self.assertRaises(SystemExit) as ctx:
            agy_send.pick(rows, "同名")
        message = str(ctx.exception)
        self.assertIn("宛先を1つに絞れません", message)
        self.assertIn("conv-app-0001", message)
        self.assertIn("conv-app-0002", message)

        with self.assertRaises(SystemExit) as ctx:
            agy_send.pick(rows, "見つからない名前")
        self.assertIn("該当なし", str(ctx.exception))

    def test_list_tsv_limit_and_tab_in_title(self) -> None:
        make_db(
            self.app_db,
            [
                dict(APP_ROW, conversation_id="conv-app-0001", title="改行\nあり\tタブ", last_modified_time=200),
                dict(APP_ROW, conversation_id="conv-app-0002", title="二番目", last_modified_time=100),
            ],
        )
        code, out = run_main(["list"])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "IDLE\t3\t200\t改行 あり タブ\tconv-app-0001")
        self.assertEqual(len(lines), 2)

        code, out = run_main(["list", "--limit", "1"])
        self.assertEqual(len(out.strip().splitlines()), 1)

        code, out = run_main(["list", "--json"])
        data = json.loads(out)
        self.assertEqual(data[0]["conversation_id"], "conv-app-0001")

    def test_send_app_builds_command(self) -> None:
        make_db(self.app_db, [APP_ROW])
        with mock.patch.object(agy_send.subprocess, "run", return_value=completed()) as run:
            code, out = run_main(["send", "conv-app-0001", "本文", "--title", "件名"])

        self.assertEqual(code, 0)
        self.assertIn("送信受付: conv-app-0001 アプリ会話1", out)
        self.assertEqual(
            run.call_args.args[0],
            ["agentapi", "send-message", "--title", "件名", "conv-app-0001", "本文"],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertIs(run.call_args.kwargs["stdin"], agy_send.subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertTrue(run.call_args.kwargs["text"])

    def test_send_app_json_and_failure(self) -> None:
        make_db(self.app_db, [APP_ROW])
        with mock.patch.object(agy_send.subprocess, "run", return_value=completed()):
            code, out = run_main(["send", "conv-app-0001", "本文", "--json"])
        self.assertEqual(json.loads(out)["conversation_id"], "conv-app-0001")
        self.assertTrue(json.loads(out)["accepted"])

        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(returncode=2, stderr="拒否されました")
        ) as run:
            with self.assertRaises(SystemExit) as ctx:
                agy_send.main(["send", "conv-app-0001", "本文"])
        self.assertIn("送信できませんでした", str(ctx.exception))
        self.assertIn("拒否されました", str(ctx.exception))
        self.assertEqual(run.call_count, 1)  # 自動再送しない

    def test_send_timeout_is_reported(self) -> None:
        make_db(self.app_db, [APP_ROW])
        timeout_error = agy_send.subprocess.TimeoutExpired(cmd="agentapi", timeout=30)
        with mock.patch.object(agy_send.subprocess, "run", side_effect=timeout_error):
            with self.assertRaises(SystemExit) as ctx:
                agy_send.main(["send", "conv-app-0001", "本文"])
        self.assertIn("自動再送はしません", str(ctx.exception))

    def test_agentapi_failure_hint_for_send_and_new(self) -> None:
        make_db(self.app_db, [APP_ROW])
        make_db(self.cli_db, [CLI_ROW])
        cases = (
            (["send", "conv-app-0001", "本文"], "Operation not permitted", "", True),
            (["new", "本文"], "別のエラー", "PERMISSION DENIED", True),
            (["send", "conv-cli-0001", "本文", "--cli"], "Operation not permitted", "", True),
            (["send", "conv-app-0001", "本文"], "connection refused", "", False),
            (["new", "本文"], "connection refused", "", False),
        )
        for argv, stderr, stdout, expected_hint in cases:
            with self.subTest(argv=argv, stderr=stderr, stdout=stdout):
                with mock.patch.object(
                    agy_send.subprocess, "run",
                    return_value=completed(returncode=1, stderr=stderr, stdout=stdout),
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        agy_send.main(argv)
                message = str(ctx.exception)
                self.assertIn("終了コード 1", message)
                self.assertEqual('sandbox_permissions="require_escalated"' in message, expected_hint)

    def test_send_cli_builds_command(self) -> None:
        make_db(self.cli_db, [CLI_ROW])
        payload = {"conversation_id": "conv-cli-0001", "response": "了解"}
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(stdout=json.dumps(payload))
        ) as run:
            code, out = run_main(
                ["send", "conv-cli-0001", "本文", "--cli", "--model", "m1", "--cwd", str(self.tmp)]
            )

        self.assertEqual(code, 0)
        self.assertEqual(
            run.call_args.args[0],
            [
                "agy",
                "-p",
                "本文",
                "--conversation",
                "conv-cli-0001",
                "--output-format",
                "json",
                "--print-timeout",
                "300s",
                "--model",
                "m1",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 300 + agy_send.CLI_TIMEOUT_GRACE)
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.tmp))
        self.assertIn("conversation_id: conv-cli-0001", out)
        self.assertIn("了解", out)

    def test_cli_skip_permissions_is_opt_in(self) -> None:
        make_db(self.cli_db, [CLI_ROW])
        payload = {"conversation_id": "conv-cli-0001", "response": "了解"}
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(stdout=json.dumps(payload))
        ) as run:
            code, _ = run_main(
                ["send", "conv-cli-0001", "本文", "--cli", "--skip-permissions"]
            )
        self.assertEqual(code, 0)
        self.assertIn("--dangerously-skip-permissions", run.call_args.args[0])

    def test_send_cli_json_and_bad_output(self) -> None:
        make_db(self.cli_db, [CLI_ROW])
        payload = {"conversation_id": "conv-cli-0001", "response": "了解"}
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(stdout=json.dumps(payload))
        ):
            code, out = run_main(["send", "conv-cli-0001", "本文", "--cli", "--json"])
        self.assertEqual(json.loads(out), {"conversation_id": "conv-cli-0001", "response": "了解"})

        with mock.patch.object(agy_send.subprocess, "run", return_value=completed(stdout="not json")):
            with self.assertRaises(SystemExit) as ctx:
                agy_send.main(["send", "conv-cli-0001", "本文", "--cli"])
        self.assertIn("JSON を解釈できません", str(ctx.exception))

    def test_new_app_accepts_both_typo_and_fixed_keys(self) -> None:
        for key, conv_id in (("newConversationn", "conv-new-0001"), ("newConversation", "conv-new-0002")):
            payload = {"response": {key: {"conversationId": conv_id}}}
            with mock.patch.object(
                agy_send.subprocess, "run", return_value=completed(stdout=json.dumps(payload))
            ):
                code, out = run_main(["new", "最初の本文", "--title", "新規"])
            self.assertEqual(code, 0)
            self.assertIn(f"conversationId: {conv_id}", out)

    def test_new_app_builds_command_and_json(self) -> None:
        payload = {"response": {"newConversationn": {"conversationId": "conv-new-0001"}}}
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(stdout=json.dumps(payload))
        ) as run:
            code, out = run_main(["new", "最初の本文", "--model", "m1", "--title", "新規", "--json"])

        self.assertEqual(
            run.call_args.args[0],
            ["agentapi", "new-conversation", "--model", "m1", "--title", "新規", "最初の本文"],
        )
        self.assertEqual(json.loads(out), {"conversation_id": "conv-new-0001"})

    def test_new_app_without_conversation_id_exits(self) -> None:
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(stdout='{"response": {}}')
        ):
            with self.assertRaises(SystemExit) as ctx:
                agy_send.main(["new", "最初の本文"])
        self.assertIn("新規会話IDを取得できません", str(ctx.exception))

    def test_new_cli_builds_command(self) -> None:
        payload = {"conversation_id": "conv-cli-0002", "response": "開始しました"}
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(stdout=json.dumps(payload))
        ) as run:
            code, out = run_main(["new", "最初の本文", "--cli", "--json"])

        self.assertEqual(
            run.call_args.args[0],
            [
                "agy",
                "-p",
                "最初の本文",
                "--output-format",
                "json",
                "--print-timeout",
                "300s",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 300 + agy_send.CLI_TIMEOUT_GRACE)
        self.assertEqual(json.loads(out), {"conversation_id": "conv-cli-0002", "response": "開始しました"})

    def make_title_collision(self, title: str = "同名相談") -> None:
        """アプリと CLI に同じタイトルの会話を1件ずつ作る。"""
        make_db(self.app_db, [dict(APP_ROW, conversation_id="conv-app-0001", title=title)])
        make_db(self.cli_db, [dict(CLI_ROW, conversation_id="conv-cli-0001", title=title)])

    def assert_collision_exit(self, argv: list[str], app_id: str, cli_id: str) -> None:
        with mock.patch.object(agy_send.subprocess, "run") as run:
            with self.assertRaises(SystemExit) as ctx:
                agy_send.main(argv)
        message = str(ctx.exception)
        self.assertIn("アプリ会話と CLI 会話の両方に一致する候補があります", message)
        self.assertIn(f"[app] {app_id}", message)
        self.assertIn(f"[cli] {cli_id}", message)
        self.assertEqual(run.call_count, 0)  # 衝突時は送らない

    def test_same_title_in_app_and_cli_stops_on_both_routes(self) -> None:
        self.make_title_collision()
        for argv in (
            ["send", "同名相談", "本文"],
            ["send", "同名相談", "本文", "--cli"],
        ):
            self.assert_collision_exit(argv, "conv-app-0001", "conv-cli-0001")

    def test_same_prefix_in_app_and_cli_stops(self) -> None:
        make_db(self.app_db, [dict(APP_ROW, conversation_id="conv-shared-0001", title="アプリ")])
        make_db(self.cli_db, [dict(CLI_ROW, conversation_id="conv-shared-0002", title="CLI")])
        for argv in (
            ["send", "conv-shared", "本文"],
            ["send", "conv-shared", "本文", "--cli"],
        ):
            self.assert_collision_exit(argv, "conv-shared-0001", "conv-shared-0002")

    def test_same_title_still_sends_by_exact_conversation_id(self) -> None:
        self.make_title_collision()
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed()
        ) as run:
            code, out = run_main(["send", "conv-app-0001", "本文"])
        self.assertEqual(code, 0)
        self.assertIn("送信受付: conv-app-0001", out)
        self.assertEqual(run.call_args.args[0][-2:], ["conv-app-0001", "本文"])

        payload = {"conversation_id": "conv-cli-0001", "response": "了解"}
        with mock.patch.object(
            agy_send.subprocess, "run", return_value=completed(stdout=json.dumps(payload))
        ) as run:
            code, out = run_main(["send", "conv-cli-0001", "本文", "--cli"])
        self.assertEqual(code, 0)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--conversation") + 1], "conv-cli-0001")
        self.assertIn("了解", out)

    def test_invalid_option_combinations_exit_without_running(self) -> None:
        cases = [
            (["send", "conv-app-0001", "本文", "--model", "m1"], "アプリ会話への send"),
            (["send", "conv-app-0001", "本文", "--cwd", str(self.tmp)], "アプリ会話への send"),
            (["send", "conv-app-0001", "本文", "--skip-permissions"], "アプリ会話への send"),
            (["new", "本文", "--cwd", str(self.tmp)], "アプリ会話の new"),
            (["new", "本文", "--skip-permissions"], "アプリ会話の new"),
            (["send", "conv-cli-0001", "本文", "--cli", "--title", "件名"], "agy CLI では --title"),
            (["new", "本文", "--cli", "--title", "件名"], "agy CLI では --title"),
            (["send", "conv-app-0001", "本文", "--timeout", "0"], "--timeout は 1 以上"),
            (["new", "本文", "--cli", "--timeout", "-1"], "--timeout は 1 以上"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                with mock.patch.object(agy_send.subprocess, "run") as run:
                    with self.assertRaises(SystemExit) as ctx:
                        agy_send.main(argv)
                    self.assertIn(expected, str(ctx.exception))
                    self.assertEqual(run.call_count, 0)

    def test_agentapi_env_reads_newest_alive_daemon_json(self) -> None:
        make_db(self.app_db, [APP_ROW])
        self.daemon_dir.mkdir(parents=True, exist_ok=True)
        (self.daemon_dir / "broken.json").write_text("{bad", encoding="utf-8")
        dead = self.daemon_dir / "hub_dead.json"
        dead.write_text(json.dumps({"pid": 999999, "httpPort": 11111, "csrfToken": "dead-tok"}), encoding="utf-8")
        old_live = self.daemon_dir / "hub_old.json"
        old_live.write_text(json.dumps({"pid": 1001, "httpPort": 22222, "csrfToken": "old-tok"}), encoding="utf-8")
        new_live = self.daemon_dir / "hub_new.json"
        new_live.write_text(json.dumps({"pid": 1002, "httpPort": 33333, "csrfToken": "new-tok"}), encoding="utf-8")
        os.utime(dead, (300, 300))
        os.utime(old_live, (100, 100))
        os.utime(new_live, (200, 200))

        def fake_kill(pid, sig):
            if pid == 999999:
                raise ProcessLookupError()
            if pid == 1001:
                raise PermissionError()

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            agy_send.os, "kill", side_effect=fake_kill
        ), mock.patch.object(agy_send.subprocess, "run", return_value=completed()) as run:
            code, out = run_main(["send", "conv-app-0001", "本文"])

        self.assertEqual(code, 0)
        self.assertIn("送信受付", out)
        passed_env = run.call_args.kwargs["env"]
        self.assertEqual(passed_env["ANTIGRAVITY_LS_ADDRESS"], "localhost:33333")
        self.assertEqual(passed_env["ANTIGRAVITY_CSRF_TOKEN"], "new-tok")

    def test_agentapi_env_exits_without_live_daemon(self) -> None:
        make_db(self.app_db, [APP_ROW])
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            agy_send.subprocess, "run"
        ) as run:
            with self.assertRaises(SystemExit) as ctx:
                agy_send.main(["send", "conv-app-0001", "本文"])
        self.assertIn("起動中プロセスが見つかりません", str(ctx.exception))
        self.assertEqual(run.call_count, 0)

    def test_agentapi_env_skips_daemon_scan_when_env_already_set(self) -> None:
        make_db(self.app_db, [APP_ROW])
        with mock.patch.object(agy_send, "_daemon_files", side_effect=AssertionError("should not scan")), mock.patch.object(
            agy_send.subprocess, "run", return_value=completed()
        ) as run:
            code, _ = run_main(["send", "conv-app-0001", "本文"])
        self.assertEqual(code, 0)
        self.assertIsNone(run.call_args.kwargs["env"])

    def test_agentapi_env_rejects_partial_explicit_config(self) -> None:
        for name in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "value"}, clear=True):
                with mock.patch.object(agy_send, "_daemon_files") as files:
                    with self.assertRaisesRegex(SystemExit, "両方指定"):
                        agy_send._agentapi_env()
                    files.assert_not_called()

class FindBinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-relay-agy-bin-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.exe = self.tmp / "agentapi"
        self.exe.write_text("#!/bin/sh\n", encoding="utf-8")
        self.exe.chmod(0o755)

    def test_env_var_wins(self) -> None:
        with mock.patch.dict(os.environ, {"TEST_AGY_BIN": str(self.exe)}):
            self.assertEqual(
                agy_send.find_bin("TEST_AGY_BIN", "agentapi", self.tmp / "none"), str(self.exe)
            )

    def test_which_then_fallback_then_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(agy_send.shutil, "which", return_value="/opt/bin/agentapi"):
                self.assertEqual(
                    agy_send.find_bin("TEST_AGY_BIN", "agentapi", self.exe), "/opt/bin/agentapi"
                )
            with mock.patch.object(agy_send.shutil, "which", return_value=None):
                self.assertEqual(
                    agy_send.find_bin("TEST_AGY_BIN", "agentapi", self.exe), str(self.exe)
                )
                with self.assertRaises(SystemExit) as ctx:
                    agy_send.find_bin("TEST_AGY_BIN", "agentapi", self.tmp / "none")
        self.assertIn("見つかりません", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
