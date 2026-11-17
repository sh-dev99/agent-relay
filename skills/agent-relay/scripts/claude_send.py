#!/usr/bin/env python3
"""起動中の Claude Code セッションへ、外部プロセスからメッセージを1通送る。

使い方:
  python3 claude_send.py list
  python3 claude_send.py send <宛先> "<本文>"
  python3 claude_send.py send <宛先> --sender Codex < 本文.txt
宛先はセッション名、PID、sessionId（先頭一致可）のどれか。

状態:
  会話が未ロード    -> アプリで開いて起こし、受信ソケットができ次第送る
  送れた            -> 終了コード 0（到達確認は arrival を見る）
  送れなかった      -> 終了コード 3（アプリ停止・起こせない・宛先不明）
  送信結果が不明    -> 終了コード 1（再送しないこと）
"""
import argparse
import glob
import json
import mmap
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from relay_app_guard import app_closed_message, app_process_running
from relay_state import check_duplicate, record_state

SESSIONS = os.path.expanduser("~/.claude/sessions")
PROJECTS = os.path.expanduser("~/.claude/projects")
CLAUDE_APP_BINARY = Path("/Applications/Claude.app/Contents/MacOS/Claude")
CLAUDE_APP_NAME = "Claude アプリ"
SEND_TIMEOUT = 10
# 記録の末尾だけを見る。全会話の先頭から探すと大きな記録で遅くなる
RECORD_TAIL_BYTES = 512 * 1024
# 未ロードの会話を起こすとき、アプリが受信ソケットを作るまで待つ上限
WAKE_TIMEOUT = 10
CLAUDE_LOG_DIR = os.path.expanduser("~/Library/Logs/Claude")
# アプリは会話ごとの内部ID（local_…）をログに残す。これを深いリンクで開くと起こせる
MAPPING_RE = re.compile(
    r"Mapping internal session (local_[0-9a-fA-F-]+) to CLI session ([0-9a-fA-F-]+)"
)

NOT_LOADED_MSG = (
    "宛先の会話は記録にありますが、今は起動していません（未ロード）。"
    "Claude アプリでその会話を開くと受信ソケットができ、送れるようになります。"
)
UNKNOWN_MSG = (
    "宛先のセッションも、その会話の記録も見つかりません。"
    "ID・名前を確かめてください（未ロードの会話は、記録が残っているものだけ見分けられます）。"
)
WAKE_FAIL_MSG = {
    "no_mapping": "この会話の内部IDがログに見つからないため起こせません。"
                  "アプリでその会話を開いてから送り直してください。",
    "no_open_command": "open コマンドが見つからないため起こせません。"
                       "アプリでその会話を開いてから送り直してください。",
    "open_failed": "アプリに会話を開く指示を出せませんでした。"
                   "アプリでその会話を開いてから送り直してください。",
    "different_session": "起こした会話が別の CLI セッションとして起動しました。"
                         "宛先違いを避けるため送っていません。アプリの画面を確かめてください。",
    "timeout": "会話を起こせませんでした。アプリでその会話を開いてから送り直してください。",
}

class NotSent(Exception):
    """送らなかったことが確定した状態。"""

class UnknownResult(Exception):
    """送ったかどうか分からない状態。再送しない。"""

def session_rows():
    """~/.claude/sessions の json をすべて読む（ソケットの有無で捨てない）。"""
    out = []
    for p in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        d["_file"] = p
        d["_live"] = bool(d.get("messagingSocketPath")) and os.path.exists(
            d["messagingSocketPath"]
        )
        out.append(d)
    return out

def live_sessions():
    return [d for d in session_rows() if d["_live"]]

def record_path(session_id):
    """会話記録（jsonl）のパス。無ければ None。"""
    if not session_id:
        return None
    hits = glob.glob(os.path.join(PROJECTS, "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None

def record_lookup(target):
    """sessionId の先頭一致で会話記録を探す。見つかった sessionId を返す。"""
    if len(target) < 4:
        return []
    found = set()
    for p in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        stem = os.path.basename(p)[:-len(".jsonl")]
        if stem.startswith(target):
            found.add(stem)
    return sorted(found)

def _title_of(path):
    """会話記録に残る表示名（custom-title）。無ければ None。読めなければ OSError。"""
    size = os.path.getsize(path)
    if not size:
        return None
    with open(path, "rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            idx = mm.rfind(b'"customTitle"')
            if idx < 0:
                return None
            start = mm.rfind(b"\n", 0, idx) + 1
            end = mm.find(b"\n", idx)
            line = mm[start:end if end >= 0 else size]
    try:
        data = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    title = data.get("customTitle")
    return title.strip() if isinstance(title, str) and title.strip() else None

def record_title(path):
    """会話記録に残る表示名（custom-title）。無ければ None。"""
    try:
        return _title_of(path)
    except (OSError, ValueError):
        return None

def record_title_scan(target):
    """会話記録の表示名が一致する sessionId と、読めなかった記録の件数を返す。

    mmap の後方検索なので、実測で 585 件・2.2GB を 0.6 秒で走査できる。
    一部だけ見て候補を見落とすと同名の別会話へ誤送信しうるため、件数は絞らない。
    読めなかった記録があるときは、一意だと決めつけないための手がかりとして件数を返す。
    """
    hits = set()
    unreadable = 0
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        try:
            title = _title_of(path)
        except (OSError, ValueError):
            unreadable += 1
            continue
        if title == target:
            hits.add(os.path.basename(path)[:-len(".jsonl")])
    return sorted(hits), unreadable

def live_row_for_conversation(session_id, host_id=None):
    """同じ会話が今の別 ID で起動していれば、その起動中の行を返す。

    sessionId は再開のたびに変わるが、アプリ内部ID（local_…）は会話に付き添う。
    """
    rows = [d for d in session_rows() if d["_live"]]
    if not rows:
        return None
    host = host_id or (local_session_id(session_id) if session_id else None)
    if not host:
        return None
    for d in rows:
        if d.get("hostSessionId") == host:
            return d
    return None

def settle(row):
    """明示IDで指定された未ロードの宛先を、同じ会話の起動中の行へ寄せる。

    寄せる根拠は、セッション情報の hostSessionId か、アプリのログにある
    「Mapping internal session local_… to CLI session …」の対応。どちらも無ければ寄せない。
    """
    if row.get("_live"):
        return row
    current = live_row_for_conversation(row.get("sessionId"), row.get("hostSessionId"))
    if current is None:
        return row
    print(
        f"注: 指定の {row.get('sessionId')} は起動していません。"
        f"同じ会話として起動中の {current.get('sessionId')} に送ります。",
        file=sys.stderr,
    )
    return current

def group_by_session(rows):
    """同じ sessionId は同じ会話としてまとめる（別IDはまとめない）。"""
    groups = {}
    for d in rows:
        groups.setdefault(str(d.get("sessionId") or ""), []).append(d)
    return groups

def candidate_lines(groups):
    out = []
    for sid, group in sorted(groups.items()):
        row = next((d for d in group if d["_live"]), group[0])
        state = "起動中" if row["_live"] else "未ロード"
        out.append(f"{sid}（{state}）{row.get('name') or ''}")
    return out

def describe(row):
    return {
        "pid": row.get("pid"),
        "name": row.get("name"),
        "session_id": row.get("sessionId"),
        "status": row.get("status"),
    }

def _pick(rows):
    """行の集合から1つの会話を選ぶ。別 sessionId が混ざれば候補を出して止まる。"""
    groups = group_by_session(rows)
    if len(groups) > 1:
        sys.exit("宛先を1つに絞れません（候補）: " + " / ".join(candidate_lines(groups)))
    if not groups:
        return None
    group = next(iter(groups.values()))
    # 同じ sessionId の重複行は同じ会話。受信ソケットが生きている行を使う
    live = [d for d in group if d["_live"]]
    return settle(live[0] if live else group[0])

def _describe_ids(ids, rows):
    """候補の sessionId を、状態と名前つきで並べる。"""
    out = []
    for sid in sorted(ids):
        row = next((d for d in rows if str(d.get("sessionId")) == sid), None)
        if row is None:
            out.append(f"{sid}（記録のみ）")
            continue
        state = "起動中" if row["_live"] else "未ロード"
        out.append(f"{sid}（{state}）{row.get('name') or ''}")
    return out

def find(target):
    """宛先を1つに決める。起動中なら _live、記録だけなら not_loaded。

    PID と sessionId（先頭一致）は明示指定として扱う。名前は、セッション情報と
    会話記録の表示名を両方集めてから判断する。片方だけ見ると、稼働中の会話と
    記録だけ残った同名の別会話を見落として誤送信するため。
    """
    rows = session_rows()
    pid_hits = [d for d in rows if str(d.get("pid")) == target]
    if pid_hits:
        return _pick(pid_hits)

    name_hits = [d for d in rows if d.get("name") == target]
    id_hits = [d for d in rows if str(d.get("sessionId", "")).startswith(target)]

    if not name_hits:
        # 明示 ID として解決する（セッション情報 → 記録の ID 接頭一致）
        if id_hits:
            return _pick(id_hits)
        ids = record_lookup(target)
        if len(ids) == 1:
            return settle({
                "sessionId": ids[0],
                "name": None,
                "pid": None,
                "_live": False,
                "_record": record_path(ids[0]),
            })
        if len(ids) > 1:
            sys.exit("宛先を1つに絞れません（会話記録が複数一致）: " + " / ".join(ids))

    # 名前として解決する。セッション情報と記録の表示名を統合してから候補数を数える
    ids = {str(d.get("sessionId")) for d in name_hits + id_hits}
    ids.discard("")
    titled, unreadable = record_title_scan(target)
    ids |= set(titled)
    if len(ids) > 1:
        sys.exit("宛先を1つに絞れません（会話名の候補）: " + " / ".join(_describe_ids(ids, rows)))
    if not ids:
        print(UNKNOWN_MSG, file=sys.stderr)
        if unreadable:
            print(f"（読めない記録が {unreadable} 件あり、同名の会話が埋もれている可能性があります）",
                  file=sys.stderr)
        sys.exit(3)
    if unreadable:
        sys.exit(
            f"宛先を1つに絞りません（記録 {unreadable} 件を読めず、同名の別会話がある可能性）。"
            "sessionId を指定してください。"
        )
    sid = next(iter(ids))
    row = next((d for d in rows if str(d.get("sessionId")) == sid), None)
    if row is None:
        row = {
            "sessionId": sid,
            "name": target,
            "pid": None,
            "_live": False,
            "_record": record_path(sid),
        }
    return settle(row)

def local_session_id(session_id):
    """アプリ内部の会話ID（local_…）をログから引く。新しいログを先に見る。"""
    files = sorted(
        glob.glob(os.path.join(CLAUDE_LOG_DIR, "main*.log")),
        key=os.path.getmtime,
        reverse=True,
    )[:4]
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                hits = [m for m in MAPPING_RE.findall(fh.read()) if m[1] == session_id]
        except OSError:
            continue
        if hits:
            return hits[-1][0]
    return None

def wake_conversation(session_id, host_id, timeout=WAKE_TIMEOUT):
    """アプリで会話を開いて受信ソケットを作る。戻り値は (起動した行|None, 理由)。

    同じ会話が別の CLI セッションで起動したときは、宛先違いを避けるため送らない。
    """
    if not host_id:
        return None, "no_mapping"
    opener = shutil.which("open")
    if not opener:
        return None, "no_open_command"
    try:
        subprocess.run(
            [opener, "-g", f"claude://code/continue?session={host_id}"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "open_failed"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for d in session_rows():
            if not d["_live"]:
                continue
            if str(d.get("sessionId")) == session_id:
                return d, "opened"
            if d.get("hostSessionId") == host_id:
                return None, "different_session"
        time.sleep(0.25)
    return None, "timeout"

def send(sock_path, text):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(SEND_TIMEOUT)
    try:
        try:
            s.connect(sock_path)
        except OSError as exc:
            # 接続できていないので、まだ1バイトも送っていない
            raise NotSent(f"受信ソケットに接続できませんでした: {exc}") from exc
        # 1行のJSONを送って送信側を閉じる。応答は返らない（空）のが正常
        frame = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            s.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode())
            s.shutdown(socket.SHUT_WR)
        except OSError as exc:
            raise UnknownResult(str(exc)) from exc
    finally:
        s.close()

def _id_in_text(text, message_id):
    pattern = r"(?<![0-9A-Za-z_-])" + re.escape(message_id) + r"(?![0-9A-Za-z_-])"
    return re.search(pattern, text) is not None

def arrived_in_record(path, message_id):
    """会話記録の user 行に、同じメッセージ ID が完全一致で入っているか。"""
    if not path or not message_id:
        return False
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - RECORD_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "user":
            continue
        msg = row.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        elif isinstance(content, str):
            text = content
        else:
            continue
        if _id_in_text(text, message_id):
            return True
    return False

def build_text(sender, body, message_id):
    body = body.strip("\n")
    if sender and sender.strip():
        return f"{sender.strip()}から\nメッセージ ID: {message_id}\n\n{body}"
    # 送り主なしでも到達確認できるよう、ID だけは必ず本文へ入れる
    return f"メッセージ ID: {message_id}\n\n{body}"

def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        for d in live_sessions():
            print(d.get("pid"), d.get("status"), d.get("kind"), d.get("name"),
                  d.get("sessionId"), sep="\t")
        return 0

    parser = argparse.ArgumentParser(description="Claude Code セッションへのメッセージ送信")
    parser.add_argument("command", choices=["send"])
    parser.add_argument("target", help="セッション名・PID・sessionId（先頭一致可）")
    parser.add_argument("text", nargs="?", default=None, help="本文（省略時は標準入力）")
    parser.add_argument("--sender", default=None, help="送り主名（付けると見出し2行を追加）")
    parser.add_argument("--message-id", default=None, help="再送防止用のメッセージ ID")
    parser.add_argument("--resend", action="store_true", help="同じメッセージ ID でも再送する")
    parser.add_argument("--no-wake", action="store_true", help="未ロードの会話を起こさずに止まる")
    parser.add_argument("--wake-timeout", type=float, default=WAKE_TIMEOUT,
                        help="未ロードの会話を起こすときの待ち時間（秒）")
    args = parser.parse_args()

    body = args.text if args.text is not None else sys.stdin.read()
    if not body.strip():
        parser.error("本文が空です")

    message_id = args.message_id or str(uuid.uuid4())

    row = find(args.target)
    session_id = row.get("sessionId")
    woke = None
    if not row["_live"]:
        if not app_process_running(CLAUDE_APP_BINARY):
            print(app_closed_message(CLAUDE_APP_NAME), file=sys.stderr)
            print(f"sessionId: {session_id}  name: {row.get('name')}", file=sys.stderr)
            return 3
        if args.no_wake:
            print(NOT_LOADED_MSG, file=sys.stderr)
            print(f"sessionId: {session_id}  name: {row.get('name')}", file=sys.stderr)
            return 3
        host_id = row.get("hostSessionId") or local_session_id(session_id)
        woken, reason = wake_conversation(session_id, host_id, args.wake_timeout)
        if woken is None:
            print("宛先の会話は記録にありますが、起こせませんでした（未送信）。", file=sys.stderr)
            print(WAKE_FAIL_MSG.get(reason, NOT_LOADED_MSG), file=sys.stderr)
            print(f"sessionId: {session_id}  name: {row.get('name')}", file=sys.stderr)
            return 3
        row = woken
        woke = "opened"

    duplicate = None if args.resend else check_duplicate(session_id, message_id)
    if duplicate and not args.resend:
        print(
            f"メッセージ ID {message_id} は {duplicate.get('state')} で記録済みです。"
            "二重送信を避けるため送りません。相手の会話記録を確かめ、"
            "必要なら --resend を付けてください。",
            file=sys.stderr,
        )
        return 3

    text = build_text(args.sender, body, message_id)
    try:
        send(row["messagingSocketPath"], text)
    except NotSent as exc:
        print(f"送れませんでした（未送信）: {exc}", file=sys.stderr)
        record_state(session_id, message_id, "not_sent", "claude")
        return 3
    except UnknownResult as exc:
        print(
            f"送信結果が不明です: {exc}。メッセージ ID: {message_id}。"
            "相手の会話記録を確かめるまで再送しないでください。",
            file=sys.stderr,
        )
        record_state(session_id, message_id, "unknown", "claude")
        return 1

    path = row.get("_record") or record_path(session_id)
    arrival = "confirmed" if arrived_in_record(path, message_id) else "unconfirmed"
    record_state(session_id, message_id, "accepted", "claude")
    out = describe(row)
    out.update({
        "status": "accepted",
        "message_id": message_id,
        "arrival": arrival,
        "record": path,
    })
    if woke:
        out["wake"] = woke
    print(json.dumps(out, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
