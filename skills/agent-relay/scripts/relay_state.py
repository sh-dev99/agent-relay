"""送信の重複防止（メッセージ ID 単位の短命な記録）。

届けることを優先するため、自動で止めるのは「受付済み（accepted）」だけ。
結果不明（unknown）や未送信（not_sent）は記録を残すが、次の実行を妨げない。
排他や claim は持たない。状態は一時領域の JSON 1ファイル。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

def state_dir() -> Path:
    override = os.environ.get("AGENT_RELAY_STATE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"agent-relay-{os.getuid()}" / "state"

def _path(target: str, message_id: str) -> Path:
    digest = hashlib.sha256(f"{target}\0{message_id}".encode()).hexdigest()[:32]
    return state_dir() / f"{digest}.json"

def read_state(target: str, message_id: str) -> dict | None:
    try:
        with open(_path(target, message_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("message_id") == message_id else None

def check_duplicate(target: str, message_id: str) -> dict | None:
    """受付済みの同じメッセージだけを再送対象から外す。"""
    data = read_state(target, message_id)
    if data and data.get("state") == "accepted":
        return data
    return None

def record_state(target: str, message_id: str, state: str, route: str = "") -> bool:
    """送信後の状態を書く。保存できたかを返す。"""
    path = _path(target, message_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"target": target, "message_id": message_id,
                       "state": state, "route": route, "at": int(time.time())},
                      fh, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
