"""Codex の共有デーモン（デスクトップアプリと同じ app-server）でセッションを作る・続ける。

使い方:
  python3 codex_daemon.py new "<本文>" [--name 名前] [--cwd フォルダ] [--sandbox read-only|workspace-write]
  python3 codex_daemon.py send <threadId> "<本文>"
返事（agentMessage の本文）を表示する。ターンが終わるまで待つ（既定300秒）。
"""
import argparse
import base64
import json
import os
import socket
import struct
import time

SOCK = os.path.expanduser("~/.codex/app-server-control/app-server-control.sock")

class WS:
    """Unix ソケット上の最小の WebSocket クライアント"""

    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(SOCK)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.s.recv(4096)
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]
        self.id = 0

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = b"\x81" + bytes([0x80 | n])
        elif n < 65536:
            head = b"\x81" + bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head = b"\x81" + bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError("デーモンとの接続が切れました")
            self.buf += chunk

    def recv(self, timeout):
        self.s.settimeout(timeout)
        self._need(2)
        n, off = self.buf[1] & 0x7F, 2
        if n == 126:
            self._need(4)
            n, off = struct.unpack(">H", self.buf[2:4])[0], 4
        elif n == 127:
            self._need(10)
            n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
        self._need(off + n)
        payload, opcode = self.buf[off:off + n], self.buf[0] & 0x0F
        self.buf = self.buf[off + n:]
        return json.loads(payload) if opcode == 1 else {}

    def call(self, method, params, timeout=60):
        self.id += 1
        self.send({"id": self.id, "method": method, "params": params})
        while True:
            msg = self.recv(timeout)
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise SystemExit(f"{method} 失敗: {msg['error']}")
                return msg.get("result", {})

    def turn(self, tid, text, timeout):
        self.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": text}]})
        out, end = [], time.time() + timeout
        while time.time() < end:
            msg = self.recv(max(1, end - time.time()))
            item = msg.get("params", {}).get("item", {})
            if msg.get("method") == "item/completed" and item.get("type") == "agentMessage":
                out.append(item.get("text", ""))
            if msg.get("method") == "turn/completed":
                return out
        raise SystemExit("時間内にターンが終わりませんでした（送信自体は済んでいます）")

def connect():
    ws = WS()
    ws.call("initialize", {"clientInfo": {"name": "agent-relay", "title": "agent-relay", "version": "1"}})
    ws.send({"method": "initialized"})
    return ws

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["new", "send"])
    ap.add_argument("args", nargs="+")
    ap.add_argument("--name")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--sandbox", default="read-only")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    ws = connect()
    if a.cmd == "new":
        r = ws.call("thread/start", {"cwd": a.cwd, "sandbox": a.sandbox, "approvalPolicy": "never"})
        tid = r["thread"]["id"]
        if a.name:
            ws.call("thread/name/set", {"threadId": tid, "name": a.name})
        text = a.args[0]
    else:
        tid, text = a.args[0], a.args[1]
        ws.call("thread/resume", {"threadId": tid})
    print("threadId:", tid)
    for t in ws.turn(tid, text, a.timeout):
        print(t)
