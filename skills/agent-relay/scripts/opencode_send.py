#!/usr/bin/env python3
"""OpenCode の既存セッションへ文章を送る（デスクトップアプリが開いているときだけ）。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from relay_app_guard import ensure_app_running

OPENCODE_APP_BINARY = Path("/Applications/OpenCode.app/Contents/MacOS/OpenCode")
OPENCODE_APP_NAME = "OpenCode.app"

def build_text(sender: str | None, body: str, message_id: str) -> str:
    body = body.strip("\n")
    if sender and sender.strip():
        sender = sender.strip()
        return f"{sender}から\nメッセージ ID: {message_id}\n\n{body}"
    return body

def read_body(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    data = sys.stdin.read()
    if not data.strip():
        raise ValueError("本文が空です")
    return data

def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode セッションへメッセージを送る")
    parser.add_argument("session_id", help="宛先セッション ID")
    parser.add_argument("text", nargs="?", default=None, help="本文（省略時は標準入力）")
    parser.add_argument("--sender", default=None, help="送り主名（付けると見出し行を付与）")
    args = parser.parse_args()

    ensure_app_running(OPENCODE_APP_BINARY, OPENCODE_APP_NAME)

    try:
        body = read_body(args)
    except ValueError as exc:
        parser.error(str(exc))

    message_id = str(uuid.uuid4())
    text = build_text(args.sender, body, message_id)
    payload = json.dumps({"text": text}, ensure_ascii=False)

    try:
        result = subprocess.run(
            [
                "opencode",
                "api",
                "session.prompt",
                f"--param",
                f"sessionID={args.session_id}",
                "-d",
                payload,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"送信に失敗しました: {exc}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"終了コード {result.returncode}"
        print(f"送信に失敗しました: {detail}", file=sys.stderr)
        return 1

    out: dict = {"message_id": message_id, "session_id": args.session_id}
    raw = result.stdout.strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                out["response"] = parsed
                if "delivery" in parsed:
                    out["delivery"] = parsed["delivery"]
        except ValueError:
            out["response_raw"] = raw

    print(json.dumps(out, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())