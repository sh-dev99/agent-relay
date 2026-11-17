#!/usr/bin/env python3
"""Desktop 所有チャットへ外部 AI の文章を届ける。"""
import argparse
import glob
import json
import os
import re
from pathlib import Path
import socket
import stat
import struct
import shutil
import subprocess
import sys
import time
import uuid

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from relay_app_guard import app_closed_message, app_process_running
from relay_state import check_duplicate, record_state

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
RECORD_TAIL_BYTES = 256 * 1024
# 未ロードのチャットを開いて、アプリが持ち主を認識するまで待つ上限
WAKE_TIMEOUT = 10

def thread_record(thread):
    """スレッドの会話記録（rollout）のパス。無ければ None。"""
    base = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"
    hits = glob.glob(str(base / "**" / f"*{thread}.jsonl"), recursive=True)
    if not hits:
        return None
    return max(hits, key=os.path.getmtime)

def _id_in_text(text, message_id):
    pattern = r"(?<![0-9A-Za-z_-])" + re.escape(message_id) + r"(?![0-9A-Za-z_-])"
    return re.search(pattern, text) is not None

def arrived_in_record(record, message_id):
    """会話記録の user 入力に、同じメッセージ ID が完全一致で入っているか。"""
    if not record or not message_id:
        return False
    try:
        size = os.path.getsize(record)
        with open(record, "rb") as fh:
            fh.seek(max(0, size - RECORD_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get("payload") or {}
        if payload.get("role") != "user" and payload.get("type") != "user_message":
            continue
        content = payload.get("content")
        if isinstance(content, list):
            text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        else:
            text = str(payload.get("message") or content or "")
        if _id_in_text(text, message_id):
            return True
    return False

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

    def initialize(self):
        response = self.request("initialize", {"clientType": "agent-relay"}, 0)
        result = response.get("result")
        client_id = result.get("clientId") if isinstance(result, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise RelayError("IPC 接続先を確認できません")
        self.client_id = client_id

    def find_owner(self, thread):
        """チャットの持ち主。無ければ OwnerNotFound。"""
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
        return owner

    def deliver(self, thread, payload, message_id):
        self.initialize()
        owner = self.find_owner(thread)
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

def _check_socket(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RelayError("IPC ソケットの所有者またはアクセス権を確認できません")

def steer(path, thread, payload, message_id):
    _check_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(REQUEST_TIMEOUT)
        sock.connect(str(path))
        Client(sock).deliver(thread, payload, message_id)

def owner_exists(path, thread):
    _check_socket(path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(REQUEST_TIMEOUT)
        sock.connect(str(path))
        client = Client(sock)
        client.initialize()
        try:
            client.find_owner(thread)
            return True
        except OwnerNotFound:
            return False

def wake_thread(path, thread, timeout=WAKE_TIMEOUT):
    """未ロードのチャットをアプリで開き、持ち主が付くまで待つ。"""
    opener = shutil.which("open")
    if not opener:
        return False
    try:
        subprocess.run([opener, "-g", f"codex://threads/{thread}"],
                       check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if owner_exists(path, thread):
                return True
        except (OSError, RelayError):
            return False
        time.sleep(0.25)
    return False

def codex_app_process_running():
    """Codex デスクトップアプリ（ChatGPT.app）のプロセスが存在するか。"""
    return app_process_running(CODEX_APP_BINARY)

def ensure_codex_app_running():
    if not codex_app_process_running():
        print(app_closed_message(CODEX_APP_NAME), file=sys.stderr)
        sys.exit(3)

def owner_exists(path, thread):
    """チャットがアプリに読み込まれているか（送信はしない）。"""
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RelayError("IPC ソケットの所有者またはアクセス権を確認できません")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(REQUEST_TIMEOUT)
        sock.connect(str(path))
        client = Client(sock)
        client.initialize()
        client.find_owner(thread)
    return True

def wake_thread(path, thread, timeout=WAKE_TIMEOUT):
    """未ロードのチャットをアプリで開き、持ち主が付くまで待つ。"""
    opener = shutil.which("open")
    if not opener:
        return False
    try:
        subprocess.run([opener, "-g", f"codex://threads/{thread}"],
                       check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if owner_exists(path, thread):
                return True
        except OwnerNotFound:
            pass
        except (OSError, RelayError):
            return False
        time.sleep(0.25)
    return False

def deliver_message(path, thread, sender, body, message_id, *, steer_only=False, visible=False, wake=False):
    payload = f"{sender}から\nメッセージ ID: {message_id}\n\n{body}"
    owner_not_found = False
    woke = False

    def accepted(route):
        out = {"status": "accepted", "route": route,
               "presentation": "chat_message", "message_id": message_id}
        if woke:
            out["wake"] = "opened"
        return out

    if not visible:
        try:
            steer(path, thread, payload, message_id)
            return accepted("steer")
        except InactiveTurn:
            if steer_only:
                raise
        except OwnerNotFound:
            if steer_only:
                raise
            if wake and wake_thread(path, thread):
                woke = True
                try:
                    steer(path, thread, payload, message_id)
                    return accepted("steer")
                except InactiveTurn:
                    pass
                except OwnerNotFound:
                    print(OWNER_NOT_FOUND_WARNING, file=sys.stderr)
                    owner_not_found = True
            else:
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
    out = {"status": "queued", "route": "queue", "presentation": "chat_message", "message_id": message_id}
    if owner_not_found:
        out["fallback_reason"] = "owner_not_found"
    if woke:
        out["wake"] = "opened"
    return out

def main():
    parser = argparse.ArgumentParser(description="Desktop チャットへの外部 AI メッセージ送信")
    parser.add_argument("--thread", required=True, help="宛先チャットの UUID")
    parser.add_argument("--sender", required=True, help="送信元の名前")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--steer-only", action="store_true", help="作業中のターンへの差し込みだけを試す")
    mode.add_argument("--visible", action="store_true", help="チャット入力としてキューに送る")
    parser.add_argument("--no-wake", action="store_true", help="未ロードのチャットを起こさずに止まる")
    parser.add_argument("--message-id", default=None, help="再送防止用のメッセージ ID")
    parser.add_argument("--resend", action="store_true", help="同じメッセージ ID でも再送する")
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
    message_id = args.message_id or str(uuid.uuid4())
    duplicate = None if args.resend else check_duplicate(thread, message_id)
    if duplicate and not args.resend:
        print(
            f"メッセージ ID: {message_id} は {duplicate.get('state')} で記録済みです。"
            "二重送信を避けるため送りません。記録を確かめ、必要なら --resend を付けてください。",
            file=sys.stderr,
        )
        return 3
    ensure_codex_app_running()
    path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "ipc" / "ipc.sock"
    try:
        result = deliver_message(path, thread, sender, body, message_id,
                                 steer_only=args.steer_only, visible=args.visible,
                                 wake=not args.no_wake)
        record = thread_record(thread)
        result["record"] = record
        result["arrival"] = "confirmed" if arrived_in_record(record, message_id) else "unconfirmed"
        record_state(thread, message_id, "accepted", "codex")
        print(json.dumps(result, ensure_ascii=False))
    except NotSent as exc:
        print(f"メッセージ ID: {message_id}。{exc}", file=sys.stderr)
        record_state(thread, message_id, "not_sent", "codex")
        return 3
    except (OSError, ValueError, KeyError, RelayError) as exc:
        print(f"メッセージ ID: {message_id}。送信を完了できません: {exc}", file=sys.stderr)
        record_state(thread, message_id, "unknown", "codex")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
