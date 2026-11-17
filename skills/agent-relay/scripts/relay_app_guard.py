"""宛先デスクトップアプリの起動確認（標準ライブラリのみ）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

def app_process_running(app_binary: Path) -> bool:
    """指定の .app バイナリを実行しているプロセスが存在するか。"""
    binary = Path(app_binary)
    r = subprocess.run(
        ["pgrep", "-x", binary.name],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False
    binary_str = str(binary)
    for pid in r.stdout.split():
        pid = pid.strip()
        if not pid:
            continue
        proc = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and binary_str in proc.stdout:
            return True
    return False

def app_closed_message(app_display_name: str) -> str:
    """アプリ未起動時に標準エラーへ出す定型文。"""
    return (
        f"宛先アプリが閉じています：{app_display_name} が起動していません。"
        f"{app_display_name} を開いてから送り直してください。"
    )

def ensure_app_running(app_binary: Path, app_display_name: str | None = None) -> None:
    """宛先アプリが無ければ送らず終了コード 3 で止める。"""
    name = app_display_name or f"{app_binary.parent.parent.name}"
    if not app_process_running(app_binary):
        print(app_closed_message(name), file=sys.stderr)
        sys.exit(3)