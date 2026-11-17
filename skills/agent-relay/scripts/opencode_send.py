#!/usr/bin/env python3
"""OpenCode の既存セッションへ文章を送る（デスクトップアプリが開いているときだけ）。

画面で開いていないセッションにも届く。送信そのものが新しいターンを起こす。
状態:
  送れた          -> 終了コード 0（受付の証拠は response のユーザーメッセージ）
  送れなかった    -> 終了コード 3（アプリ停止・セッション不明・API エラー）
  送信結果が不明  -> 終了コード 1（再送しないこと）
"""
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
from relay_state import check_duplicate, record_state

OPENCODE_APP_BINARY = Path("/Applications/OpenCode.app/Contents/MacOS/OpenCode")
OPENCODE_APP_NAME = "OpenCode.app"
REQUEST_TIMEOUT = 120

def build_text(sender: str | None, body: str, message_id: str) -> str:
    body = body.strip("\n")
    if sender and sender.strip():
        sender = sender.strip()
        return f"{sender}から\nメッセージ ID: {message_id}\n\n{body}"
    return f"メッセージ ID: {message_id}\n\n{body}"

def read_body(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    data = sys.stdin.read()
    if not data.strip():
        raise ValueError("本文が空です")
    return data

def classify_error(detail: str) -> str:
    if "SessionNotFoundError" in detail:
        return "宛先のセッションが見つかりません。ID を確かめてください。"
    return f"送信に失敗しました: {detail}"

def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode セッションへメッセージを送る")
    parser.add_argument("session_id", help="宛先セッション ID")
    parser.add_argument("text", nargs="?", default=None, help="本文（省略時は標準入力）")
    parser.add_argument("--sender", default=None, help="送り主名（付けると見出し行を付与）")
    parser.add_argument("--message-id", default=None, help="再送防止用のメッセージ ID")
    parser.add_argument("--resend", action="store_true", help="同じメッセージ ID でも再送する")
    args = parser.parse_args()

    ensure_app_running(OPENCODE_APP_BINARY, OPENCODE_APP_NAME)

    try:
        body = read_body(args)
    except ValueError as exc:
        parser.error(str(exc))

    message_id = args.message_id or str(uuid.uuid4())
    duplicate = None if args.resend else check_duplicate(args.session_id, message_id)
    if duplicate and not args.resend:
        print(
            f"メッセージ ID {message_id} は {duplicate.get('state')} で記録済みです。"
            "二重送信を避けるため送りません。会話を確かめ、必要なら --resend を付けてください。",
            file=sys.stderr,
        )
        return 3

    text = build_text(args.sender, body, message_id)
    payload = json.dumps({"text": text}, ensure_ascii=False)

    try:
        result = subprocess.run(
            [
                "opencode",
                "api",
                "session.prompt",
                "--param",
                f"sessionID={args.session_id}",
                "-d",
                payload,
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=REQUEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        print(
            f"送信結果が不明です: 待機期限を超えました。メッセージ ID: {message_id}。"
            "会話を確かめるまで再送しないでください。",
            file=sys.stderr,
        )
        record_state(args.session_id, message_id, "unknown", "opencode")
        return 1
    except OSError as exc:
        print(f"送れませんでした（未送信）: {exc}", file=sys.stderr)
        record_state(args.session_id, message_id, "not_sent", "opencode")
        return 3

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"終了コード {result.returncode}"
        print(classify_error(detail), file=sys.stderr)
        record_state(args.session_id, message_id, "not_sent", "opencode")
        return 3

    out: dict = {"status": "accepted", "message_id": message_id, "session_id": args.session_id}
    journey = "unconfirmed"
    raw = result.stdout.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            out["response"] = parsed
            if "delivery" in parsed:
                out["delivery"] = parsed["delivery"]
            data = parsed.get("data")
            if isinstance(data, dict):
                if data.get("id"):
                    out["message_object_id"] = data["id"]
                    journey = "confirmed"
                payload_text = (data.get("payload") or {}).get("text")
                if isinstance(payload_text, str) and message_id in payload_text:
                    journey = "confirmed"
        else:
            out["response_raw"] = raw

    out["arrival"] = journey
    record_state(args.session_id, message_id, "accepted", "opencode")
    print(json.dumps(out, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
