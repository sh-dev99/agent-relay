#!/usr/bin/env python3
"""Grok Bot の Routine（agent-inbox）を Webhook で起動する。

使い方:
  python3 grokbot_send.py send "依頼本文" [--from 自分のアプリ名] [--force]

依頼本文は ~/Library/Application Support/agent-relay/inbox/grokbot.md に書き出し、Webhook は呼び鈴として叩く。
URL と key はキーチェーン（サービス名 grokbot-webhook-url / grokbot-webhook-key、アカウント agent-relay）から読む。
画面やログには出さない。
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from relay_app_guard import app_closed_message, app_process_running, ensure_app_running

INBOX = Path.home() / "Library" / "Application Support" / "agent-relay" / "inbox" / "grokbot.md"
GROK_BOT_BINARY = Path("/Applications/Grok Bot.app/Contents/MacOS/Grok Bot")
GROK_BOT_APP_NAME = "Grok Bot.app"
LOCAL_EXEC_LOG = Path.home() / ".grokbot" / "local-exec-daemon.log"
GROK_DISCONNECTED_MSG = (
    f"宛先アプリが閉じています：{GROK_BOT_APP_NAME} が Mac とつながっていません。"
    f"{GROK_BOT_APP_NAME} を開いてから送り直してください。"
)

# local-exec-daemon.log の接続状態を判別する行パターン（時刻は UTC）
_WATCH_CONNECTED = re.compile(r"\[user-computer-watch\].*\bconnected after\b")
_WATCH_DISCONNECTED = re.compile(
    r"\[user-computer-watch\].*(?:ended error after|closed after)\b"
)
_DAEMON_STARTED = re.compile(r"\[sand-local-exec-daemon\] started\b")
_DAEMON_SIGTERM = re.compile(r"\[sand-local-exec-daemon\] received SIGTERM\b")

def keychain(service):
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", "agent-relay", "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"キーチェーンに {service} がありません")
    return r.stdout.strip()

def grok_bot_process_running():
    """Grok Bot.app 本体のプロセスが存在するか。"""
    return app_process_running(GROK_BOT_BINARY)

def last_local_exec_connection_state(log_path=LOCAL_EXEC_LOG):
    """ログ末尾の接続系イベントが connected なら True、切断系なら False。

    読めない・解析不能のときは None（呼び出し側で警告のみにする）。
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    last_kind = None
    for line in text.splitlines():
        if _WATCH_CONNECTED.search(line):
            last_kind = "connected"
        elif _WATCH_DISCONNECTED.search(line):
            last_kind = "disconnected"
        elif _DAEMON_STARTED.search(line):
            last_kind = "daemon_started"
        elif _DAEMON_SIGTERM.search(line):
            last_kind = "daemon_stopped"

    if last_kind is None:
        return False
    return last_kind == "connected"

def ensure_grok_bot_reachable(*, force=False, log_path=LOCAL_EXEC_LOG, warn=sys.stderr):
    """送信前に Grok Bot が Mac とつながっている想定かを確かめる。駄目なら終了コード 3。"""
    if force:
        return
    if not grok_bot_process_running():
        print(app_closed_message(GROK_BOT_APP_NAME), file=sys.stderr)
        sys.exit(3)
    state = last_local_exec_connection_state(log_path)
    if state is None:
        print(
            "注意: Grok Bot の local-exec ログを読めないため、Mac との接続は確認できません。"
            "そのまま送信します。",
            file=warn,
        )
        return
    if not state:
        print(GROK_DISCONNECTED_MSG, file=sys.stderr)
        sys.exit(3)

def send(text, sender, *, force=False, log_path=LOCAL_EXEC_LOG):
    ensure_grok_bot_reachable(force=force, log_path=log_path)
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    INBOX.write_text(f"# 依頼（from: {sender} / {stamp}）\n\n{text}\n", encoding="utf-8")
    body = json.dumps({"from": sender, "task": "grokbot.md を読んで作業して"}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        keychain("grokbot-webhook-url"),
        data=body,
        headers={
            "Authorization": f"Bearer {keychain('grokbot-webhook-key')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            print(res.status, res.read().decode("utf-8", "replace")[:300])
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("text")
    s.add_argument("--from", dest="sender", default="unknown")
    s.add_argument(
        "--force",
        action="store_true",
        help="Grok Bot の起動・接続確認をせずに送る",
    )
    a = p.parse_args()
    send(a.text, a.sender, force=a.force)