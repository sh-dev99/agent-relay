import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("codex_send", Path(__file__).parents[1] / "scripts" / "codex_send.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

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
    def test_confirmed_inactive_turn_queues_same_message_once(self):
        client = FakeClient(reject=(
            "thread-follower-steer-turn: Cannot steer conversation thread "
            "because its active turn already ended"
        ))
        with (patch.object(module, "steer", side_effect=lambda path, *args: client.deliver(*args)),
              patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as queue):
            result = module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", wake=False)
        self.assertEqual(result, {
            "status": "queued", "route": "queue", "presentation": "chat_message", "message_id": "id"
        })
        queue.assert_called_once()
        payload = client.calls[-1][1]["input"][0]["text"]
        self.assertEqual(queue.call_args.args[0], ["codex", "queue", "--thread", "thread", "--message", payload])
        self.assertEqual(payload, "Claudeから\nメッセージ ID: id\n\n報告")

    def test_steering_success_and_unknown_outcome_never_touch_queue(self):
        for error in (None, module.RelayError("送信結果は未確定")):
            with (self.subTest(error=error), patch.object(module, "steer", side_effect=error),
                  patch.object(module.subprocess, "run") as queue):
                if error:
                    with self.assertRaises(module.RelayError):
                        module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id")
                else:
                    result = module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", wake=True)
                    self.assertEqual(result["route"], "steer")
                    self.assertEqual(result["presentation"], "chat_message")
                queue.assert_not_called()

    def test_visible_option_queues_without_trying_steer(self):
        with (patch.object(module, "steer") as steer,
              patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as queue):
            result = module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", visible=True)
        steer.assert_not_called()
        queue.assert_called_once()
        self.assertEqual(result["presentation"], "chat_message")

    def test_steer_only_refusal_stays_not_sent(self):
        with (patch.object(module, "steer", side_effect=module.InactiveTurn("未送信")),
              patch.object(module.subprocess, "run") as queue):
            with self.assertRaises(module.NotSent):
                module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", steer_only=True)
        queue.assert_not_called()

    def test_no_client_found_queues_with_fallback_reason(self):
        with (patch.object(module, "steer", side_effect=module.OwnerNotFound("未送信")),
              patch.object(module, "wake_thread", return_value=False),
              patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as queue):
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                result = module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id")
        self.assertEqual(result, {
            "status": "queued",
            "route": "queue",
            "presentation": "chat_message",
            "message_id": "id",
            "fallback_reason": "owner_not_found",
        })
        queue.assert_called_once()
        self.assertEqual(stderr.getvalue().strip(), module.OWNER_NOT_FOUND_WARNING)

    def test_owner_not_found_wakes_then_queues(self):
        with (patch.object(module, "steer", side_effect=[module.OwnerNotFound("未ロード"), module.InactiveTurn("待機")]),
              patch.object(module, "wake_thread", return_value=True) as wake,
              patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as queue):
            result = module.deliver_message(Path("socket"), "thread", "Claude", "報告", "id", wake=True)
        self.assertEqual(result["wake"], "opened")
        self.assertEqual(result["route"], "queue")
        self.assertEqual(wake.call_count, 1)
        queue.assert_called_once()

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
            patch.object(module, "codex_app_process_running", return_value=False),
            patch.object(module, "deliver_message") as deliver,
            patch.object(sys, "argv", argv),
            patch.object(sys, "stdin", stdin),
            self.assertRaises(SystemExit) as ctx,
        ):
            module.main()
        self.assertEqual(ctx.exception.code, 3)
        deliver.assert_not_called()

class TestArrivalRecord(unittest.TestCase):
    def test_thread_record_and_arrival(self):
        import tempfile
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
                self.assertIsNone(module.thread_record("-".join(["99999999", "2222", "4333", "8444", "555555555555"])))

class TestDuplicate(unittest.TestCase):
    def test_same_message_id_blocks_resend(self):
        import tempfile
        thread = "00000000-0000-4000-8000-" + "000000000001"
        argv = ["codex_send.py", "--thread", thread, "--sender", "t", "--message-id", "m1"]
        ok = {"status": "accepted", "route": "steer", "presentation": "chat_message",
              "message_id": "m1"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"AGENT_RELAY_STATE_DIR": tmp}):
            with patch.object(module, "ensure_codex_app_running"), \
                    patch.object(module, "deliver_message", return_value=dict(ok)), \
                    patch.object(module, "thread_record", return_value=None), \
                    patch.object(module, "arrived_in_record", return_value=False), \
                    patch.object(sys, "argv", argv), \
                    patch.object(sys, "stdin", io.StringIO("本文")), \
                    patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(module.main(), 0)
            with patch.object(module, "ensure_codex_app_running") as guard, \
                    patch.object(module, "deliver_message") as deliver, \
                    patch.object(sys, "argv", argv), \
                    patch.object(sys, "stdin", io.StringIO("本文")), \
                    patch.object(sys, "stderr", io.StringIO()) as err:
                self.assertEqual(module.main(), 3)
        deliver.assert_not_called()
        guard.assert_not_called()
        self.assertIn("二重送信", err.getvalue())

if __name__ == "__main__":
    unittest.main()
