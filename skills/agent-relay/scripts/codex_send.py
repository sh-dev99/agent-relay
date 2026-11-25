#!/usr/bin/env python3
"""Desktop 所有チャットへ外部 AI の文章を届ける。"""
import argparse
import glob
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
from relay_state import read_state, record_state

MAX_FRAME = 16 * 1024 * 1024
REQUEST_TIMEOUT = 45
# アプリの thread-owner-discovery は応答まで約10秒かかることがある
OWNER_DISCOVERY_MIN_WAIT_SEC = 10
CODEX_APP_BINARY = Path("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT")
CODEX_APP_NAME = "Codex アプリ（ChatGPT.app）"
RECORD_TAIL_BYTES = 256 * 1024
APP_SERVER_SOCKET_ENV = "CODEX_APP_SERVER_SOCKET"

def thread_record(thread):
    """ローカルの会話記録のパス。無ければ None、複数あれば送信を止める。"""
    base = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"
    hits = glob.glob(str(base / "**" / f"*-{thread}.jsonl"), recursive=True)
    if not hits:
        return None
    if len(hits) > 1:
        raise NotSent("ローカルの会話記録が複数あり、宛先を一つに特定できません。未送信です")
    return hits[0]

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
    marker = f"メッセージ ID: {message_id}"
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
        if marker in text.splitlines():
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

class AppServerResponseError(RelayError):
    def __init__(self, method, code, message):
        super().__init__(f"App Server が {method} にエラーを返しました（コード {code}）")
        self.method = method
        self.code = code
        self.message = message

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
        try:
            self.initialize()
            owner = self.find_owner(thread)
        except NotSent:
            raise
        except (OSError, ValueError, KeyError, RelayError) as exc:
            raise NotSent(f"IPC の送信準備に失敗しました。未送信です: {exc}") from exc
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
        raise NotSent("IPC ソケットの所有者またはアクセス権を確認できず、未送信です")

def steer(path, thread, payload, message_id):
    try:
        _check_socket(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(REQUEST_TIMEOUT)
            sock.connect(str(path))
            Client(sock).deliver(thread, payload, message_id)
    except OSError as exc:
        raise NotSent(f"ローカル IPC に接続できず、未送信です: {exc}") from exc

def codex_app_process_running():
    """Codex デスクトップアプリ（ChatGPT.app）のプロセスが存在するか。"""
    return app_process_running(CODEX_APP_BINARY)

def ensure_codex_app_running():
    if not codex_app_process_running():
        print(app_closed_message(CODEX_APP_NAME), file=sys.stderr)
        sys.exit(3)

def deliver_message(path, thread, sender, body, message_id, *, steer_only=False):
    payload = f"{sender}から\nメッセージ ID: {message_id}\n\n{body}"
    if steer_only:
        steer(path, thread, payload, message_id)
        return {"status": "accepted", "route": "steer",
                "presentation": "chat_message", "message_id": message_id}

    try:
        result = subprocess.run(
            ["codex", "queue", "--thread", thread, "--message", payload],
            check=False, capture_output=True, text=True, errors="replace", timeout=REQUEST_TIMEOUT,
        )
    except (OSError, ValueError) as exc:
        raise NotSent(f"codex queue を起動できず、未送信です: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RelayError("キュー投入結果は未確定: 待機期限を超えました。自動再送しないでください") from exc
    except KeyboardInterrupt as exc:
        raise RelayError("キュー投入結果は未確定: 実行が中断されました。自動再送しないでください") from exc
    if result.returncode:
        detail = result.stderr.strip() or f"終了コード {result.returncode}"
        raise RelayError(f"キュー投入結果は未確定: {detail}。自動再送しないでください")
    return {"status": "queued", "route": "queue", "message_id": message_id}

def app_server_request(connection, request_id, method, params):
    connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
    deadline = time.monotonic() + REQUEST_TIMEOUT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{method} の応答待ち時間を超えました")
        frame = connection.recv(timeout=remaining)
        response = json.loads(frame) if isinstance(frame, str) else None
        if not isinstance(response, dict):
            raise RelayError(f"{method} の応答形式が不正です")
        if "method" in response or type(response.get("id")) is not int or response["id"] != request_id:
            continue
        if "error" not in response and isinstance(response.get("result"), dict):
            return response["result"]
        error = response.get("error")
        if ("result" not in response and isinstance(error, dict)
                and type(error.get("code")) is int and isinstance(error.get("message"), str)):
            raise AppServerResponseError(method, error["code"], error["message"])
        raise RelayError(f"{method} の応答形式が不正です")

def app_server_thread_status(connection, request_id, thread):
    summary = app_server_request(connection, request_id, "thread/read", {"threadId": thread})["thread"]
    if summary["id"] != thread:
        raise NotSent("接続先の応答が指定したチャット ID と一致しません。未送信です")
    status = summary["status"]["type"]
    if status not in ("active", "idle"):
        raise NotSent("接続先で指定したチャットが読み込み済みの状態と確認できません。未送信です")
    return status

def app_server_active_turn(connection, request_id, thread, expected_turn):
    turns = app_server_request(connection, request_id, "thread/turns/list", {
        "threadId": thread, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded",
    })["data"]
    if not isinstance(turns, list) or len(turns) != 1 or turns[0]["status"] != "inProgress":
        raise NotSent("指定したチャットの実行中ターンを接続先で確認できません。未送信です")
    turn = turns[0]["id"]
    if not isinstance(turn, str) or not turn.strip() or (expected_turn is not None and turn != expected_turn):
        raise NotSent("指定したターンが接続先で実行中と確認できません。未送信です")
    return turn

def deliver_app_server(path, thread, expected_turn, sender, read_body, message_id):
    if not path.is_absolute():
        raise NotSent("認可済み App Server の Unix ソケットを絶対パスで指定してください。未送信です")
    try:
        is_socket = path.is_socket()
    except OSError as exc:
        raise NotSent("指定された App Server の Unix ソケットを確認できません。未送信です") from exc
    if not is_socket:
        raise NotSent("指定されたパスに App Server の Unix ソケットが見つかりません。未送信です")
    try:
        from websockets.exceptions import WebSocketException
        from websockets.sync.client import unix_connect
    except ImportError as exc:
        raise NotSent("公式 Unix ソケット経路には Python の websockets 11 以降が必要です。未送信です") from exc

    send_started = False
    try:
        with unix_connect(str(path), open_timeout=REQUEST_TIMEOUT, close_timeout=2) as connection:
            app_server_request(connection, 1, "initialize", {
                "clientInfo": {"name": "agent_relay", "title": "Agent Relay", "version": "1.0"},
            })
            connection.send(json.dumps({"method": "initialized", "params": {}}))
            status = app_server_thread_status(connection, 2, thread)
            if expected_turn is not None:
                if status != "active":
                    raise NotSent("接続先で指定したチャットが実行中ではありません。未送信です")
                app_server_active_turn(connection, 3, thread, expected_turn)

            payload = f"{sender}から\nメッセージ ID: {message_id}\n\n{read_body()}"
            status = app_server_thread_status(connection, 4, thread)
            if expected_turn is not None and status != "active":
                raise NotSent("接続先で指定したチャットが実行中ではありません。未送信です")
            turn = app_server_active_turn(connection, 5, thread, expected_turn) if status == "active" else None
            params = {"threadId": thread, "input": [{"type": "text", "text": payload}]}
            if turn is not None:
                method = "turn/steer"
                params["expectedTurnId"] = turn
            else:
                method = "turn/start"
            send_started = True
            try:
                result = app_server_request(connection, 6, method, params)
            except AppServerResponseError as exc:
                if method == "turn/steer" and exc.code == -32600 and (
                    exc.message == "no active turn to steer"
                    or exc.message.startswith(f"expected active turn id `{turn}` but found `")
                    or exc.message in ("cannot steer a review turn", "cannot steer a compact turn")
                ):
                    raise NotSent("App Server がターンへの追記を拒否しました。未送信です") from exc
                if method == "turn/start" and exc.code == -32600 and exc.message == f"thread not found: {thread}":
                    raise NotSent("接続先 App Server に指定のチャットが読み込まれていません。未送信です") from exc
                raise
            if method == "turn/steer":
                if result.get("turnId") != turn:
                    raise RelayError("App Server の受理応答で送信対象のターン ID を確認できません")
            else:
                turn = result["turn"]["id"]
                if not isinstance(turn, str) or not turn.strip():
                    raise RelayError("App Server の受理応答で送信対象のターン ID を確認できません")
    except NotSent:
        raise
    except AppServerResponseError as exc:
        if send_started:
            raise RelayError(f"{exc}。送信の結果は不明です。自動再送しないでください") from exc
        if exc.method == "thread/read" and exc.code == -32600 and exc.message == f"thread not loaded: {thread}":
            raise NotSent("接続先 App Server に指定のチャットが読み込まれていません。未送信です") from exc
        if (exc.method == "thread/turns/list" and exc.code == -32600
                and exc.message == "ephemeral threads do not support thread/turns/list"):
            raise NotSent("一時チャットのターン一覧を取得できず、実行中のターンを照合できません。未送信です") from exc
        raise NotSent(f"{exc}。未送信です") from exc
    except (OSError, ValueError, KeyError, TypeError, RelayError, WebSocketException, KeyboardInterrupt) as exc:
        if send_started:
            raise RelayError("App Server への送信の結果は不明です。自動再送しないでください") from exc
        raise NotSent(f"App Server への送信準備を完了できません。未送信です（{type(exc).__name__}）") from exc
    route = "app_server_steer" if method == "turn/steer" else "app_server_start"
    return {"status": "accepted", "route": route, "turn_id": turn,
            "presentation": "chat_message", "message_id": message_id,
            "arrival": "unconfirmed", "read": "unconfirmed"}

def main():
    parser = argparse.ArgumentParser(description="既存の Codex チャットへ送信（既定は公式の codex queue）")
    parser.add_argument("--thread", required=True, help="宛先チャットの UUID")
    parser.add_argument("--host-id", choices=("local", "durable"),
                        help="宛先のホスト ID。durable は送信未対応。Desktop 宛てはローカル記録で照合")
    parser.add_argument("--sender", required=True, help="送信元の名前")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--steer-only", action="store_true", help="既存の非公開 Desktop 経路で差し込みだけ試す")
    mode.add_argument("--visible", action="store_true", help="従来の指定。既定と同じく codex queue に投入する")
    mode.add_argument("--app-server", action="store_true",
                      help=f"{APP_SERVER_SOCKET_ENV} にある認可済みの公式 App Server Unix ソケットを使う")
    mode.add_argument("--app-server-socket", type=Path,
                      help=f"認可済みの公式 App Server Unix ソケット。{APP_SERVER_SOCKET_ENV} より優先")
    parser.add_argument("--expected-turn-id", help="公式経路の送信を指定した実行中のターンに限定する")
    parser.add_argument("--no-wake", action="store_true", help="従来の指定。チャットを自動で開く処理は廃止済み")
    parser.add_argument("--message-id", default=None, help="再送防止用のメッセージ ID")
    parser.add_argument("--resend", action="store_true", help="同じメッセージ ID でも再送する")
    args = parser.parse_args()
    app_server_socket = args.app_server_socket
    if args.expected_turn_id is not None and (args.steer_only or args.visible or not args.expected_turn_id.strip()):
        parser.error("--expected-turn-id には実行中ターンの ID が必要です（--steer-only・--visible と併用不可）")
    if app_server_socket is None and (args.app_server or args.expected_turn_id is not None):
        configured_socket = os.environ.get(APP_SERVER_SOCKET_ENV)
        if not configured_socket:
            parser.error(f"公式ソケットには --app-server-socket または {APP_SERVER_SOCKET_ENV} の設定が必要です")
        app_server_socket = Path(configured_socket)
    try:
        thread = str(uuid.UUID(args.thread))
        if thread != args.thread.lower():
            raise ValueError
    except ValueError:
        parser.error("thread はハイフン付き UUID で指定してください")
    try:
        if args.host_id == "durable":
            raise NotSent("このスクリプトは durable ホストへの送信に対応していません。本文は未送信です")
        if app_server_socket is None:
            record = thread_record(thread)
            if not record:
                raise NotSent("ローカルの会話記録が見つからず、宛先の所在を確認できません。未送信です")
    except NotSent as exc:
        print(f"hostId={args.host_id or '未指定'}, threadId={thread}。{exc}", file=sys.stderr)
        return 3
    sender = args.sender.strip()
    if not sender:
        parser.error("送信元と本文が必要です")

    def read_body():
        body = sys.stdin.read()
        if not body.strip():
            parser.error("送信元と本文が必要です")
        return body

    if app_server_socket is None:
        body = read_body()
    message_id = args.message_id or str(uuid.uuid4())
    previous = read_state(thread, message_id) or {}
    already_attempted = previous.get("state") in ("accepted", "unknown")
    if already_attempted and not args.resend:
        print(
            f"メッセージ ID: {message_id} は {previous['state']} で記録済みです。"
            "二重送信を避けるため送りません。記録を確かめ、必要なら --resend を付けてください。",
            file=sys.stderr,
        )
        return 3
    try:
        if app_server_socket is not None:
            result = deliver_app_server(app_server_socket, thread, args.expected_turn_id,
                                        sender, read_body, message_id)
        else:
            ensure_codex_app_running()
            path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "ipc" / "ipc.sock"
            result = deliver_message(path, thread, sender, body, message_id, steer_only=args.steer_only)
    except NotSent as exc:
        print(f"メッセージ ID: {message_id}。{exc}", file=sys.stderr)
        if not already_attempted:
            record_state(thread, message_id, "not_sent", "codex")
        return 3
    except (OSError, ValueError, KeyError, RelayError) as exc:
        print(f"メッセージ ID: {message_id}。送信を完了できません: {exc}", file=sys.stderr)
        record_state(thread, message_id, "unknown", "codex")
        return 1
    record_state(thread, message_id, "accepted", "codex")
    result["host_id"] = "local"
    result["thread_id"] = thread
    if app_server_socket is None:
        result["record"] = record
        result["arrival"] = "confirmed" if arrived_in_record(record, message_id) else "unconfirmed"
    print(json.dumps(result, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
