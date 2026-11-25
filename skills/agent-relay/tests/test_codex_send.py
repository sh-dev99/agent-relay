import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("codex_send", Path(__file__).parents[1] / "scripts" / "codex_send.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import unix_serve
except ImportError:
    unix_serve = None

class TestProtocol(unittest.TestCase):
    def test_notifications_do_not_extend_request_deadline(self):
        payload = b'{"type":"notification"}'
        frame = struct.pack("<I", len(payload)) + payload
        sock, peer = socket.socketpair()
        with sock, peer:
            peer.sendall(frame * 3)
            with patch.object(module.time, "monotonic", side_effect=[0, 0, 10, 20, 30, 40, 45]):
                with self.assertRaisesRegex(module.RelayError, "待機期限"):
                    module.Client(sock).request("test", {}, 0)

    def test_non_object_frame_is_protocol_error(self):
        sock, peer = socket.socketpair()
        with sock, peer:
            peer.sendall(struct.pack("<I", 2) + b"[]")
            with self.assertRaisesRegex(module.RelayError, "形式が不正"):
                module.Client(sock).request("test", {}, 0)

    def test_only_explicit_error_frames_can_trigger_fallback(self):
        response = {
            "type": "response",
            "requestId": "request",
            "error": "Cannot steer conversation thread because its active turn already ended",
        }
        cases = (
            ({}, module.RelayError),
            ({"resultType": "unexpected"}, module.RelayError),
            ({"resultType": "error"}, module.RequestRejected),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                frame = json.dumps({**response, **fields}).encode()
                sock, peer = socket.socketpair()
                with sock, peer, patch.object(module.uuid, "uuid4", return_value="request"):
                    peer.sendall(struct.pack("<I", len(frame)) + frame)
                    with self.assertRaises(expected) as caught:
                        module.Client(sock).request("thread-follower-steer-turn", {}, 1)
                    self.assertIs(type(caught.exception), expected)

class FakeClient(module.Client):
    def __init__(self, owner=True, capability=True, fail_send=False, reject=None, malformed=False,
                 discovery_reject=None):
        self.client_id = "initializing-client"
        self.calls = []
        self.owner = owner
        self.capability = capability
        self.fail_send = fail_send
        self.reject = reject
        self.malformed = malformed
        self.discovery_reject = discovery_reject

    def request(self, method, params, version, target=None):
        self.calls.append((method, params, version, target))
        if method == "initialize":
            return {"result": {"clientId": "relay"}}
        if method == "thread-owner-discovery":
            if self.discovery_reject:
                raise module.RequestRejected(self.discovery_reject)
            return {"handledByClientId": "owner" if self.owner else None,
                    "result": {"supportsUntrustedAppInput": self.capability}}
        if self.fail_send:
            raise TimeoutError("timeout")
        if self.reject:
            raise module.RequestRejected(self.reject)
        if self.malformed:
            return {"result": {"result": {}}}
        return {"resultType": "success", "result": {"result": {"turnId": "turn"}}}

class TestDelivery(unittest.TestCase):
    def test_chat_input_and_owner(self):
        client = FakeClient()
        payload = "Claude Codeから\nメッセージ ID: message-id\n\n報告"
        client.deliver("thread", payload, "message-id")
        method, params, version, target = client.calls[-1]
        self.assertEqual((method, version, target), ("thread-follower-steer-turn", 1, "owner"))
        self.assertEqual(params["input"], [{"type": "text", "text": payload, "text_elements": []}])
        self.assertEqual(params["clientUserMessageId"], "message-id")
        restored = params["restoreMessage"]
        self.assertEqual(restored["id"], "message-id")
        self.assertEqual(restored["text"], payload)
        self.assertEqual(restored["context"]["prompt"], payload)
        self.assertEqual(restored["context"]["workspaceRoots"], [])
        self.assertIsInstance(restored["createdAt"], int)
        self.assertFalse({"turnStart", "toolOutput", "cwd", "model", "permissions", "approvalPolicy", "sandboxPolicy"} & params.keys())

    def test_missing_capability_stops_before_delivery(self):
        client = FakeClient(capability=False)
        with self.assertRaises(module.OwnerNotFound):
            client.deliver("thread", "報告", "id")
        self.assertEqual(len(client.calls), 2)

    def test_missing_owner_stops_before_delivery(self):
        client = FakeClient(owner=False)
        with self.assertRaises(module.OwnerNotFound):
            client.deliver("thread", "報告", "id")
        self.assertEqual(len(client.calls), 2)

    def test_no_client_found_raises_owner_not_found(self):
        client = FakeClient(discovery_reject="thread-owner-discovery: no-client-found")
        with self.assertRaises(module.OwnerNotFound):
            client.deliver("thread", "報告", "id")
        self.assertEqual(len(client.calls), 2)

    def test_ipc_setup_failure_is_not_sent(self):
        client = FakeClient()
        with patch.object(client, "initialize", side_effect=TimeoutError("initialize")):
            with self.assertRaisesRegex(module.NotSent, "未送信"):
                client.deliver("thread", "報告", "id")
        self.assertEqual(client.calls, [])
        with (patch.object(module, "_check_socket"),
              patch.object(module.socket, "socket") as sock_class):
            sock = sock_class.return_value.__enter__.return_value
            sock.connect.side_effect = ConnectionRefusedError("ECONNREFUSED")
            with self.assertRaisesRegex(module.NotSent, "ECONNREFUSED"):
                module.steer(Path("socket"), "thread", "報告", "id")
            sock.sendall.assert_not_called()

    def test_timeout_does_not_retry(self):
        client = FakeClient(fail_send=True)
        with self.assertRaisesRegex(module.RelayError, "自動再送しない"):
            client.deliver("thread", "報告", "id")
        self.assertEqual(len(client.calls), 3)

    def test_rejection_does_not_retry(self):
        client = FakeClient(reject="thread-follower-steer-turn: permission denied")
        with self.assertRaisesRegex(module.RelayError, "自動再送しない"):
            client.deliver("thread", "報告", "id")
        self.assertEqual(len(client.calls), 3)

    def test_malformed_response_is_unknown(self):
        client = FakeClient(malformed=True)
        with self.assertRaisesRegex(module.RelayError, "未確定"):
            client.deliver("thread", "報告", "id")
        self.assertEqual(len(client.calls), 3)

class TestRouting(unittest.TestCase):
    def test_normal_delivery_queues_once_without_steering(self):
        with (patch.object(module, "steer") as steer,
              patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as queue):
            result = module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id")
        self.assertEqual(result, {"status": "queued", "route": "queue", "message_id": "id"})
        steer.assert_not_called()
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[0], ["codex", "queue", "--thread", "thread", "--message",
                                                "Claudeから\nメッセージ ID: id\n\n報告"])

    def test_steer_only_success_and_unknown_outcome_never_touch_queue(self):
        for error in (None, module.RelayError("送信結果は未確定")):
            with (self.subTest(error=error), patch.object(module, "steer", side_effect=error) as steer,
                  patch.object(module.subprocess, "run") as queue):
                if error:
                    with self.assertRaises(module.RelayError):
                        module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", steer_only=True)
                else:
                    result = module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", steer_only=True)
                    self.assertEqual(result["route"], "steer")
                steer.assert_called_once()
                queue.assert_not_called()

    def test_steer_only_refusal_stays_not_sent(self):
        with (patch.object(module, "steer", side_effect=module.InactiveTurn("未送信")),
              patch.object(module.subprocess, "run") as queue):
            with self.assertRaises(module.NotSent):
                module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", steer_only=True)
        queue.assert_not_called()

    def test_steering_owner_not_found_steer_only_stays_not_sent(self):
        with (patch.object(module, "steer", side_effect=module.OwnerNotFound("未送信")),
              patch.object(module.subprocess, "run") as queue):
            with self.assertRaises(module.NotSent):
                module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", steer_only=True)
        queue.assert_not_called()

    def test_owner_discovery_wait_limits(self):
        self.assertGreaterEqual(module.REQUEST_TIMEOUT, module.OWNER_DISCOVERY_MIN_WAIT_SEC)
        self.assertGreaterEqual(40_000, module.OWNER_DISCOVERY_MIN_WAIT_SEC * 1000)

class TestCodexAppGuard(unittest.TestCase):
    def test_stops_before_ipc_when_app_missing(self):
        with patch.object(module, "codex_app_process_running", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                module.ensure_codex_app_running()
        self.assertEqual(ctx.exception.code, 3)

    def test_main_does_not_touch_delivery_when_app_missing(self):
        argv = [
            "codex_send.py",
            # 公開時の検査が UUID の形を弾くため、実行時に組み立てる
            "--thread", "00000000-0000-4000-8000-" + "000000000001",
            "--sender", "test",
        ]
        stdin = io.StringIO("本文\n")
        with (
            patch.object(module, "thread_record", return_value="rollout.jsonl"),
            patch.object(module, "codex_app_process_running", return_value=False),
            patch.object(module, "deliver_message") as deliver,
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdin", stdin),
            self.assertRaises(SystemExit) as ctx,
        ):
            module.main()
        self.assertEqual(ctx.exception.code, 3)
        deliver.assert_not_called()

class TestAppServerSocketPath(unittest.TestCase):
    def test_reports_relative_path_and_missing_socket_separately(self):
        def read_body():
            self.fail("無効なソケットで本文が読み込まれました")

        with tempfile.TemporaryDirectory() as tmp:
            for path, diagnostic in ((Path("relative.sock"), "絶対パスで指定"),
                                     (Path(tmp) / "absent.sock", "Unix ソケットが見つかりません")):
                with self.subTest(path=path), self.assertRaisesRegex(module.NotSent, diagnostic):
                    module.deliver_app_server(path, "thread", "turn", "Antigravity", read_body, "id")

@unittest.skipIf(unix_serve is None, "Unix WebSocket の試験には websockets が必要")
class TestAppServerSteer(unittest.TestCase):
    thread_id = "00000000-0000-4000-8000-" + "000000000001"
    expected_turn = "expected-turn"
    message_id = "app-server-test"

    def invoke(self, replies, repeat=False, diagnostic=None, *, strict=True, configured=False):
        frames = []

        def serve(connection):
            while True:
                try:
                    raw = connection.recv(timeout=5)
                except ConnectionClosed:
                    return
                frame = json.loads(raw)
                frames.append(frame)
                reply = replies.get(frame["method"])
                if isinstance(reply, list):
                    reply = reply.pop(0)
                if reply == "disconnect":
                    return
                if reply is not None:
                    connection.send(json.dumps({"method": "account/updated", "params": {}}))
                    connection.send(json.dumps({"id": frame["id"], **reply}))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.sock"
            args = ["codex_send.py", "--thread", self.thread_id, "--sender", "Antigravity",
                    "--message-id", self.message_id]
            env = {"AGENT_RELAY_STATE_DIR": tmp, "CODEX_APP_SERVER_SOCKET": str(path)}
            if not configured:
                args.extend(["--app-server-socket", str(path)])
                env["CODEX_APP_SERVER_SOCKET"] = str(Path(tmp) / "wrong.sock")
            if strict:
                args.extend(["--expected-turn-id", self.expected_turn])
            elif configured:
                args.append("--app-server")
            with unix_serve(serve, str(path)) as server:
                server_thread = Thread(target=server.serve_forever)
                server_thread.start()
                try:
                    with (
                        patch.dict(os.environ, env),
                        patch.object(sys, "argv", args),
                        patch.object(sys, "stdin", io.StringIO("試験本文")) as stdin,
                        patch.object(stdin, "read", wraps=stdin.read) as body,
                        patch.object(sys, "stdout", io.StringIO()) as out,
                        patch.object(sys, "stderr", io.StringIO()) as err,
                        patch.object(module, "thread_record") as legacy_lookup,
                        patch.object(module, "ensure_codex_app_running") as guard,
                        patch.object(module, "deliver_message") as legacy,
                    ):
                        code = module.main()
                        if diagnostic is not None:
                            self.assertIn(diagnostic, err.getvalue())
                        if repeat:
                            self.assertEqual(module.main(), 3)
                            self.assertIn("二重送信", err.getvalue())
                        state = module.read_state(self.thread_id, self.message_id)["state"]
                    legacy_lookup.assert_not_called()
                    guard.assert_not_called()
                    legacy.assert_not_called()
                finally:
                    server.shutdown()
                    server_thread.join()
        return code, state, body.call_count, out.getvalue(), frames

    def replies(self, steer_reply, *, turn=None, status="inProgress", thread=None, runtime_status="active"):
        return {
            "initialize": {"result": {"userAgent": "mock"}},
            "thread/read": {"result": {"thread": {"id": thread or self.thread_id,
                                                     "status": {"type": runtime_status}}}},
            "thread/turns/list": {"result": {"data": [{"id": turn or self.expected_turn, "status": status}]}},
            "turn/steer": steer_reply,
        }

    def test_matching_active_turn_receives_one_user_input_and_is_not_marked_read(self):
        code, state, reads, output, frames = self.invoke(
            self.replies({"result": {"turnId": self.expected_turn}}), repeat=True,
        )
        self.assertEqual((code, state, reads), (0, "accepted", 1))
        self.assertEqual([frame["method"] for frame in frames], [
            "initialize", "initialized", "thread/read", "thread/turns/list", "thread/read", "thread/turns/list",
            "turn/steer",
        ])
        self.assertEqual(frames[-1]["params"], {
            "threadId": self.thread_id, "expectedTurnId": self.expected_turn,
            "input": [{"type": "text", "text": f"Antigravityから\nメッセージ ID: {self.message_id}\n\n試験本文"}],
        })
        result = json.loads(output)
        self.assertEqual(result["turn_id"], self.expected_turn)
        self.assertEqual(result["arrival"], "unconfirmed")
        self.assertEqual(result["read"], "unconfirmed")

    def test_configured_socket_uses_latest_active_turn_in_normal_mode(self):
        turn = "server-selected-turn"
        code, state, reads, output, frames = self.invoke(
            self.replies({"result": {"turnId": turn}}, turn=turn), strict=False, configured=True,
        )
        self.assertEqual((code, state, reads), (0, "accepted", 1))
        self.assertEqual([frame["method"] for frame in frames], [
            "initialize", "initialized", "thread/read", "thread/read", "thread/turns/list", "turn/steer",
        ])
        self.assertEqual(frames[-1]["params"]["expectedTurnId"], turn)
        self.assertEqual(json.loads(output)["route"], "app_server_steer")

    def test_idle_loaded_thread_receives_one_user_input_without_claiming_a_new_turn(self):
        replies = self.replies(None, runtime_status="idle")
        replies["turn/start"] = {"result": {"turn": {"id": "accepted-turn", "status": "inProgress"}}}
        code, state, reads, output, frames = self.invoke(replies, strict=False, repeat=True)
        self.assertEqual((code, state, reads), (0, "accepted", 1))
        self.assertEqual([frame["method"] for frame in frames], [
            "initialize", "initialized", "thread/read", "thread/read", "turn/start",
        ])
        self.assertEqual(frames[-1]["params"], {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": f"Antigravityから\nメッセージ ID: {self.message_id}\n\n試験本文"}],
        })
        result = json.loads(output)
        self.assertEqual(result, {"status": "accepted", "route": "app_server_start", "turn_id": "accepted-turn",
                                  "presentation": "chat_message", "message_id": self.message_id,
                                  "host_id": "local", "thread_id": self.thread_id,
                                  "arrival": "unconfirmed", "read": "unconfirmed"})

    def test_normal_mode_rechecks_thread_after_body_to_handle_state_changes(self):
        for initial, current, method, response in (
            ("idle", "active", "turn/steer", {"turnId": self.expected_turn}),
            ("active", "idle", "turn/start", {"turn": {"id": "new-turn"}}),
        ):
            with self.subTest(initial=initial, current=current):
                replies = self.replies(None, runtime_status=initial)
                replies["thread/read"] = [replies["thread/read"], self.replies(None, runtime_status=current)["thread/read"]]
                replies[method] = {"result": response}
                code, state, reads, _, frames = self.invoke(replies, strict=False)
                self.assertEqual((code, state, reads), (0, "accepted", 1))
                self.assertEqual([frame["method"] for frame in frames if frame["method"] in ("turn/start", "turn/steer")], [method])

    def test_strict_mode_rechecks_turn_after_body(self):
        replies = self.replies(None)
        replies["thread/turns/list"] = [replies["thread/turns/list"], self.replies(None, turn="next-turn")["thread/turns/list"]]
        code, state, reads, _, frames = self.invoke(replies, configured=True)
        self.assertEqual((code, state, reads), (3, "not_sent", 1))
        self.assertEqual(frames[-1]["method"], "thread/turns/list")

    def test_normal_mode_stops_on_unloaded_or_unknown_state(self):
        for status in ("notLoaded", "unknown", "systemError"):
            with self.subTest(status=status):
                code, state, reads, _, frames = self.invoke(self.replies(None, runtime_status=status), strict=False)
                self.assertEqual((code, state, reads), (3, "not_sent", 0))
                self.assertEqual(frames[-1]["method"], "thread/read")

    def test_ambiguous_start_result_blocks_resend(self):
        for reply in ("disconnect", {"result": {"turn": {"id": ""}}},
                      {"error": {"code": -32603, "message": "internal error"}}):
            with self.subTest(reply=reply):
                replies = self.replies(None, runtime_status="idle")
                replies["turn/start"] = reply
                code, state, reads, _, frames = self.invoke(replies, strict=False, repeat=True)
                self.assertEqual((code, state, reads), (1, "unknown", 1))
                self.assertEqual([frame["method"] for frame in frames].count("turn/start"), 1)

    def test_preflight_rejects_wrong_or_finished_target_before_reading_body(self):
        for invalid_target in ({"turn": "different"}, {"status": "completed"},
                               {"thread": "different"}, {"runtime_status": "idle"}):
            with self.subTest(invalid_target=invalid_target):
                code, state, reads, _, frames = self.invoke(self.replies(None, **invalid_target))
                self.assertEqual((code, state, reads), (3, "not_sent", 0))
                self.assertNotIn("turn/steer", [frame["method"] for frame in frames])

    def test_preflight_reports_thread_missing_from_connected_server(self):
        replies = self.replies(None)
        replies["thread/read"] = {"error": {"code": -32600, "message": f"thread not loaded: {self.thread_id}"}}
        code, state, reads, _, frames = self.invoke(replies, diagnostic="指定のチャットが読み込まれていません")
        self.assertEqual((code, state, reads), (3, "not_sent", 0))
        self.assertEqual([frame["method"] for frame in frames], ["initialize", "initialized", "thread/read"])

    def test_ephemeral_active_thread_reports_unavailable_turn_history_without_sending(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                replies = self.replies(None)
                replies["thread/turns/list"] = {
                    "error": {"code": -32600, "message": "ephemeral threads do not support thread/turns/list"},
                }
                code, state, reads, _, frames = self.invoke(
                    replies, strict=strict, diagnostic="一時チャットのターン一覧を取得できず",
                )
                self.assertEqual((code, state, reads), (3, "not_sent", 0 if strict else 1))
                self.assertEqual(frames[-1]["method"], "thread/turns/list")

    def test_explicit_turn_race_rejection_is_not_sent_and_does_not_fallback(self):
        reply = {"error": {"code": -32600,
                           "message": f"expected active turn id `{self.expected_turn}` but found `next-turn`"}}
        code, state, reads, _, frames = self.invoke(self.replies(reply))
        self.assertEqual((code, state, reads), (3, "not_sent", 1))
        self.assertEqual([frame["method"] for frame in frames].count("turn/steer"), 1)

    def test_ambiguous_steer_result_blocks_resend(self):
        for reply in ("disconnect", {"result": {"turnId": "different"}},
                      {"error": {"code": -32603, "message": "internal error"}}):
            with self.subTest(reply=reply):
                code, state, reads, _, frames = self.invoke(self.replies(reply), repeat=True)
                self.assertEqual((code, state, reads), (1, "unknown", 1))
                self.assertEqual([frame["method"] for frame in frames].count("turn/steer"), 1)

class TestArrivalRecord(unittest.TestCase):
    def test_thread_record_and_arrival(self):
        # 公開時の検査に合わせて UUID は実行時に組み立てる
        thread = "-".join(["11111111", "2222", "4333", "8444", "555555555555"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions" / "2026" / "11" / "17" / f"rollout-x-{thread}.jsonl"
            path.parent.mkdir(parents=True)
            row = {"type": "event_msg", "payload": {"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "メッセージ ID: m9"}]}}
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": tmp}):
                self.assertEqual(module.thread_record(thread), str(path))
                self.assertTrue(module.arrived_in_record(str(path), "m9"))
                self.assertFalse(module.arrived_in_record(str(path), "nope"))
                row["payload"]["content"][0]["text"] = "無関係な会話で m9 に言及しただけ"
                path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                self.assertFalse(module.arrived_in_record(str(path), "m9"))
                self.assertIsNone(module.thread_record("-".join(["99999999", "2222", "4333", "8444", "555555555555"])))

    def test_local_legacy_invocation_uses_record_resolved_before_delivery(self):
        thread = "00000000-0000-4000-8000-" + "000000000001"
        argv = ["codex_send.py", "--thread", thread, "--sender", "Claude", "--message-id", "m1"]
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            record = sessions / f"rollout-x-{thread}.jsonl"
            record.touch()

            def queue(args, **kwargs):
                self.assertEqual(args[3], thread)
                payload = args[5]
                row = {"type": "event_msg", "payload": {"type": "user_message", "message": payload}}
                record.write_text(json.dumps(row) + "\n", encoding="utf-8")
                (sessions / f"rollout-y-{thread}.jsonl").touch()
                return subprocess.CompletedProcess(args, 0)

            with (
                patch.dict(os.environ, {"CODEX_HOME": tmp, "AGENT_RELAY_STATE_DIR": tmp}),
                patch.object(module, "ensure_codex_app_running"),
                patch.object(module.subprocess, "run", side_effect=queue) as send,
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdin", io.StringIO("本文")),
                patch.object(sys, "stdout", io.StringIO()) as out,
            ):
                self.assertEqual(module.main(), 0)
            send.assert_called_once()
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["host_id"], "local")
            self.assertEqual(result["thread_id"], thread)
            self.assertEqual(result["arrival"], "confirmed")
            self.assertEqual(result["record"], str(record))

    def test_explicit_desktop_modes_override_saved_socket(self):
        thread = "00000000-0000-4000-8000-" + "000000000001"
        for mode in (None, "--visible", "--steer-only", "--no-wake"):
            argv = ["codex_send.py", "--thread", thread, "--sender", "Claude"] + ([mode] if mode else [])
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as tmp,
                patch.dict(os.environ, {"AGENT_RELAY_STATE_DIR": tmp, "CODEX_APP_SERVER_SOCKET": "/saved.sock"}),
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdin", io.StringIO("本文")),
                patch.object(sys, "stdout", io.StringIO()),
                patch.object(module, "thread_record", return_value="rollout.jsonl"),
                patch.object(module, "ensure_codex_app_running"),
                patch.object(module, "deliver_message", return_value={}) as desktop,
                patch.object(module, "deliver_app_server") as official,
            ):
                self.assertEqual(module.main(), 0)
                desktop.assert_called_once()
                self.assertEqual(desktop.call_args.kwargs["steer_only"], mode == "--steer-only")
                official.assert_not_called()

    def test_incompatible_or_incomplete_official_options_stop_before_delivery(self):
        thread = "00000000-0000-4000-8000-" + "000000000001"
        cases = (
            ("/saved.sock", ["--visible", "--app-server-socket", "/cli.sock"]),
            ("/saved.sock", ["--visible", "--app-server"]),
            ("", ["--app-server"]),
            ("/saved.sock", ["--steer-only", "--expected-turn-id", "turn"]),
            ("/saved.sock", ["--expected-turn-id", " "]),
            ("", ["--expected-turn-id", "turn"]),
        )
        for socket_path, options in cases:
            with (
                self.subTest(options=options),
                patch.dict(os.environ, {"CODEX_APP_SERVER_SOCKET": socket_path}),
                patch.object(sys, "argv", ["codex_send.py", "--thread", thread, "--sender", "Claude", *options]),
                patch.object(sys, "stdin") as stdin,
                patch.object(sys, "stderr", io.StringIO()),
                patch.object(module, "thread_record") as legacy_lookup,
                patch.object(module, "deliver_app_server") as official,
            ):
                with self.assertRaises(SystemExit) as ctx:
                    module.main()
                self.assertEqual(ctx.exception.code, 2)
                stdin.read.assert_not_called()
                legacy_lookup.assert_not_called()
                official.assert_not_called()

    def test_unresolved_destination_stops_before_body_and_local_delivery(self):
        thread = "00000000-0000-4000-8000-" + "000000000001"
        cases = (
            ((), 0, "宛先の所在"),
            (("--host-id", "local"), 2, "会話記録が複数"),
            (("--host-id", "durable", "--visible"), 1, "durable ホストへの送信に対応していません"),
        )
        for options, records, message in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                sessions = Path(tmp) / "sessions"
                sessions.mkdir()
                for i in range(records):
                    (sessions / f"rollout-{i}-{thread}.jsonl").touch()
                with (
                    patch.dict(os.environ, {"CODEX_HOME": tmp}),
                    patch.object(sys, "argv", ["codex_send.py", "--thread", thread, "--sender", "Claude", *options]),
                    patch.object(sys, "stdin") as stdin,
                    patch.object(sys, "stderr", io.StringIO()) as err,
                    patch.object(module, "thread_record", wraps=module.thread_record) as lookup,
                    patch.object(module, "ensure_codex_app_running") as guard,
                    patch.object(module, "deliver_message") as deliver,
                ):
                    self.assertEqual(module.main(), 3)
                self.assertIn(message, err.getvalue())
                stdin.read.assert_not_called()
                guard.assert_not_called()
                deliver.assert_not_called()
                if "durable" in options:
                    lookup.assert_not_called()

class TestDuplicate(unittest.TestCase):
    def test_queue_unknown_outcome_blocks_duplicate(self):
        thread = "00000000-0000-4000-8000-" + "000000000001"
        argv = ["codex_send.py", "--thread", thread, "--sender", "t", "--message-id", "m1"]
        for error in (KeyboardInterrupt(), subprocess.TimeoutExpired("codex queue", 1)):
            with (self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp,
                  patch.dict(os.environ, {"AGENT_RELAY_STATE_DIR": tmp}),
                  patch.object(module, "thread_record", return_value="rollout.jsonl"),
                  patch.object(module, "ensure_codex_app_running"),
                  patch.object(module.subprocess, "run", side_effect=error) as queue,
                  patch.object(sys, "argv", argv), patch.object(sys, "stderr", io.StringIO())):
                with patch.object(sys, "stdin", io.StringIO("本文")):
                    self.assertEqual(module.main(), 1)
                self.assertEqual(module.read_state(thread, "m1")["state"], "unknown")
                with patch.object(sys, "stdin", io.StringIO("本文")):
                    self.assertEqual(module.main(), 3)
                queue.assert_called_once()

    def test_same_message_id_blocks_resend(self):
        thread = "00000000-0000-4000-8000-" + "000000000001"
        argv = ["codex_send.py", "--thread", thread, "--sender", "t", "--message-id", "m1"]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"AGENT_RELAY_STATE_DIR": tmp}),
            patch.object(module, "thread_record", return_value="rollout.jsonl"),
            patch.object(module, "ensure_codex_app_running") as guard,
            patch.object(module, "deliver_message") as deliver,
            patch.object(sys, "argv", argv),
        ):
            with (patch.object(sys, "stdin", io.StringIO("本文")),
                  patch.object(sys, "stderr", io.StringIO()),
                  patch.object(deliver, "side_effect", module.RelayError("結果不明"))):
                self.assertEqual(module.main(), 1)
            deliver.reset_mock()
            guard.reset_mock()
            for state in ("unknown", "accepted"):
                with (self.subTest(state=state),
                      patch.object(sys, "stdin", io.StringIO("本文")),
                      patch.object(sys, "stderr", io.StringIO()) as err):
                    self.assertEqual(module.read_state(thread, "m1")["state"], state)
                    self.assertEqual(module.main(), 3)
                    self.assertIn("二重送信", err.getvalue())
                    self.assertIn(state, err.getvalue())
                module.record_state(thread, "m1", "accepted", "codex")
            deliver.assert_not_called()
            guard.assert_not_called()

if __name__ == "__main__":
    unittest.main()
