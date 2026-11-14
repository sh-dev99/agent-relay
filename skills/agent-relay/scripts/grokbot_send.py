#!/usr/bin/env python3
"""Grok Bot の Routine（agent-inbox）を Webhook で起動する。

使い方:
  python3 grokbot_send.py send "依頼本文" [--from 自分のアプリ名]

依頼本文は ~/.agents/agent-inbox/grokbot.md に書き出し、Webhook は呼び鈴として叩く。
URL と key はキーチェーン（サービス名 grokbot-webhook-url / grokbot-webhook-key、アカウント agent-relay）から読む。
画面やログには出さない。
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

INBOX = Path.home() / ".agents" / "agent-inbox" / "grokbot.md"

def keychain(service):
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", "agent-relay", "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"キーチェーンに {service} がありません")
    return r.stdout.strip()

def send(text, sender):
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
    a = p.parse_args()
    send(a.text, a.sender)
