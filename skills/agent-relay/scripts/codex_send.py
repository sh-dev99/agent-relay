#!/usr/bin/env python3
"""Desktop 所有チャットへ外部 AI の文章を届ける。"""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from relay_app_guard import app_closed_message, app_process_running

MAX_FRAME = 16 * 1024 * 1024
REQUEST_TIMEOUT = 45
# アプリの thread-owner-discovery は応答まで約10秒かかることがある
OWNER_DISCOVERY_MIN_WAIT_SEC = 10
OWNER_NOT_FOUND_WARNING = (
    "Codex アプリの中で、このチャットの持ち主が見つかりませんでした。"
    "チャットがアプリで開かれていないか、アプリの登録が外れています。"
    "相手が作業中なら、今のターンが終わるまで順番待ちになります。"
    "何度も続くときは Codex アプリを再起動してください。"
)
CODEX_APP_BINARY = Path("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT")
CODEX_APP_NAME = "Codex アプリ（ChatGPT.app）"

class RelayError(Exception):
    pass

class RequestRejected(RelayError):
    pass

class NotSent(RelayError):
    pass

class InactiveTurn(NotSent):
    pass

class OwnerNotFound(NotSent):
    pass

class Client:
    def __init__(self, sock):
        self.sock = sock
        self.client_id = "initializing-client"

    def set_remaining_timeout(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError("IPC 応答の待機期限を超えました")
        self.sock.settimeout(remaining)

    def send(self, value, deadline):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) > MAX_FRAME:
            raise RelayError("送信データが大きすぎます")
        self.set_remaining_timeout(deadline)
        self.sock.sendall(struct.pack("<I", len(data)) + data)

    def read_exact(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            self.set_remaining_timeout(deadline)
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RelayError("接続が閉じられました")
            data.extend(chunk)
        return bytes(data)

    def request(self, method, params, version, target=None):
        deadline = time.monotonic() + REQUEST_TIMEOUT
        request_id = str(uuid.uuid4())
        self.send({"type": "request", "requestId": request_id,
                   "sourceClientId": self.client_id, "version": version,
                   "method": method, "params": params, "targetClientId": target,
                   "timeoutMs": 40000}, deadline)
        while True:
            size = struct.unpack("<I", self.read_exact(4, deadline))[0]
            if not 0 < size <= MAX_FRAME:
                raise RelayError("応答サイズが不正です")
            message = json.loads(self.read_exact(size, deadline))
            if not isinstance(message, dict):
                raise RelayError("IPC 応答の形式が不正です")
            if message.get("type") == "client-discovery-request":
                self.send({"type": "client-discovery-response", "requestId": message["requestId"],
                           "response": {"canHandle": False}}, deadline)
            elif message.get("type") == "request":
                self.send({"type": "response", "requestId": message["requestId"],
                           "resultType": "error", "error": "no-handler-for-request"}, deadline)
            elif message.get("type") == "response" and message.get("requestId") == request_id:
                result_type = message.get("resultType")
                if result_type == "success":
                    return message
                error = message.get("error")
                if result_type == "error" and isinstance(error, str) and error:
                    raise RequestRejected(f"{method}: {error}")
                raise RelayError("IPC 応答の結果形式が不正です")

    def deliver(self, thread, payload, message_id):
        response = self.request("initialize", {"clientType": "agent-relay"}, 0)
        result = response.get("result")
        client_id = result.get("clientId") if isinstance(result, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise RelayError("IPC 接続先を確認できません")
        self.client_id = client_id
        try:
            response = self.request("thread-owner-discovery", {"hostId": "local", "conversationId": thread}, 1)
        except RequestRejected as exc:
            error = str(exc)
            if error == "thread-owner-discovery: no-client-found":
                raise OwnerNotFound("チャットの持ち主が見つかりませんでした") from exc
            raise
        owner = response.get("handledByClientId")
        capability = response.get("result")
        if not isinstance(owner, str) or not owner or not isinstance(capability, dict) or capability.get("supportsUntrustedAppInput") is not True:
            raise OwnerNotFound("対応するローカル所有者を確認できません")
        params = {
            "conversationId": thread,
            "clientUserMessageId": message_id,
            "input": [{"type": "text", "text": payload, "text_elements": []}],
            "attachments": [],
            "restoreMessage": {
                "id": message_id,
                "text": payload,
                "context": {
                    "prompt": payload,
                    "addedFiles": [],
                    "fileAttachments": [],
                    "ideContext": None,
                    "imageAttachments": [],
                    "workspaceRoots": [],
                },
                "createdAt": time.time_ns() // 1_000_000,
            },
        }
        try:
            response = self.request("thread-follower-steer-turn", params, 1, owner)
            result = response.get("result")
            forwarded = result.get("result") if isinstance(result, dict) else None
            if not isinstance(forwarded, dict) or not forwarded.get("turnId"):
                raise RelayError("受付結果の形式を確認できません")
            return response
        except (OSError, ValueError, KeyError, RelayError) as exc:
            if (isinstance(exc, RequestRejected)
                    and str(exc) == f"thread-follower-steer-turn: Cannot steer conversation {thread} because its active turn already ended"):
                raise InactiveTurn("作業中のターンがなく、差し込みは未送信です") from exc
            raise RelayError(f"送信結果は未確定: {exc}。自動再送しないでください") from exc

def steer(path, thread, payload, message_id):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RelayError("IPC ソケットの所有者またはアクセス権を確認できません")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(REQUEST_TIMEOUT)
        sock.connect(str(path))
        Client(sock).deliver(thread, payload, message_id)

def codex_app_process_running():
    """Codex デスクトップアプリ（ChatGPT.app）のプロセスが存在するか。"""
    return app_process_running(CODEX_APP_BINARY)

def ensure_codex_app_running():
    if not codex_app_process_running():
        print(app_closed_message(CODEX_APP_NAME), file=sys.stderr)
        sys.exit(3)

def deliver_message(path, thread, sender, body, message_id, *, steer_only=False, visible=False):
    payload = f"{sender}から\nメッセージ ID: {message_id}\n\n{body}"
    owner_not_found = False
    if not visible:
        try:
            steer(path, thread, payload, message_id)
            return {"status": "accepted", "route": "steer", "presentation": "chat_message", "message_id": message_id}
        except InactiveTurn:
            if steer_only:
                raise
        except OwnerNotFound:
            if steer_only:
                raise
            print(OWNER_NOT_FOUND_WARNING, file=sys.stderr)
            owner_not_found = True

    try:
        result = subprocess.run(
            ["codex", "queue", "--thread", thread, "--message", payload],
            check=False, capture_output=True, text=True, errors="replace", timeout=REQUEST_TIMEOUT,
        )
    except (OSError, ValueError) as exc:
        raise NotSent(f"codex queue を起動できず、未送信です: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RelayError("キュー投入結果は未確定: 待機期限を超えました。自動再送しないでください") from exc
    if result.returncode:
        detail = result.stderr.strip() or f"終了コード {result.returncode}"
        raise RelayError(f"キュー投入結果は未確定: {detail}。自動再送しないでください")
    result = {"status": "queued", "route": "queue", "presentation": "chat_message", "message_id": message_id}
    if owner_not_found:
        result["fallback_reason"] = "owner_not_found"
    return result

def main():
    parser = argparse.ArgumentParser(description="Desktop チャットへの外部 AI メッセージ送信")
    parser.add_argument("--thread", required=True, help="宛先チャットの UUID")
    parser.add_argument("--sender", required=True, help="送信元の名前")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--steer-only", action="store_true", help="作業中のターンへの差し込みだけを試す")
    mode.add_argument("--visible", action="store_true", help="チャット入力としてキューに送る")
    args = parser.parse_args()
    try:
        thread = str(uuid.UUID(args.thread))
        if thread != args.thread.lower():
            raise ValueError
    except ValueError:
        parser.error("thread はハイフン付き UUID で指定してください")
    sender = args.sender.strip()
    body = sys.stdin.read()
    if not sender or not body.strip():
        parser.error("送信元と本文が必要です")
    ensure_codex_app_running()
    path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "ipc" / "ipc.sock"
    message_id = str(uuid.uuid4())
    try:
        result = deliver_message(path, thread, sender, body, message_id,
                                 steer_only=args.steer_only, visible=args.visible)
        print(json.dumps(result, ensure_ascii=False))
    except NotSent as exc:
        print(f"メッセージ ID: {message_id}。{exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError, KeyError, RelayError) as exc:
        print(f"メッセージ ID: {message_id}。送信を完了できません: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
