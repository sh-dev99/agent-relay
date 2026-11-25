#!/usr/bin/env python3
"""Muse ブリッジの messages/inbox へ依頼 Markdown を置く（Muse.app が開いているときだけ）。"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from relay_app_guard import ensure_app_running

MUSE_APP_BINARY = Path("/Applications/Muse.app/Contents/MacOS/Muse")
MUSE_APP_NAME = "Muse.app"
DEFAULT_INBOX = Path.home() / "dev" / "tools" / "muse-tasha-bridge" / "messages" / "inbox"

def inbox_dir() -> Path:
    raw = os.environ.get("MUSE_BRIDGE_INBOX")
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_INBOX

def build_markdown(sender: str, subject: str, body: str, reply: str) -> str:
    lines = [
        f"送り主: {sender}",
        f"件名: {subject}",
        "",
        "本文:",
        body.rstrip(),
        "",
        "返信先:",
        reply.rstrip(),
    ]
    return "\n".join(lines) + "\n"

def read_body(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    data = sys.stdin.read()
    if not data.strip():
        raise ValueError("本文が空です")
    return data

def place_message(dest_dir: Path, content: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    final_name = f"{stamp}-relay-{uuid.uuid4().hex[:8]}.md"
    tmp_name = f".{final_name}.tmp"
    tmp_path = dest_dir / tmp_name
    final_path = dest_dir / final_name
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.rename(final_path)
    return final_path

def main() -> int:
    parser = argparse.ArgumentParser(description="Muse ブリッジ inbox へ依頼を置く")
    parser.add_argument("text", nargs="?", default=None, help="本文（省略時は標準入力）")
    parser.add_argument("--sender", required=True, help="送り主の表記")
    parser.add_argument("--subject", required=True, help="件名")
    parser.add_argument("--reply", required=True, help="返信方法（返信先の書き方）")
    args = parser.parse_args()

    ensure_app_running(MUSE_APP_BINARY, MUSE_APP_NAME)

    dest = inbox_dir()
    if not dest.is_dir():
        print("ブリッジが見つかりません", file=sys.stderr)
        return 1

    try:
        body = read_body(args)
    except ValueError as exc:
        parser.error(str(exc))

    md = build_markdown(args.sender, args.subject, body, args.reply)
    path = place_message(dest, md)
    print(path.name)
    return 0

if __name__ == "__main__":
    sys.exit(main())