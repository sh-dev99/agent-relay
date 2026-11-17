"""宛先デスクトップアプリの起動確認（標準ライブラリのみ）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

def app_process_running(app_binary: Path) -> bool:
    """指定の .app バイナリを実行しているプロセスが存在するか。

    pgrep -x は実行環境によってプロセスを見落とすことがある
    （Codex アプリで実測: pgrep は該当なし、ps には出る）。
    そのためプロセス一覧を直接読み、起動パスの一致で判定する。
    """
    binary_str = str(Path(app_binary))
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError:
        return False
    if r.returncode != 0:
        return False
    return any(binary_str in line for line in r.stdout.splitlines())

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
