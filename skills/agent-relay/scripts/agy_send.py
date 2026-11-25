#!/usr/bin/env python3
"""Antigravity の会話一覧・送信・新規作成を行う。

使い方:
  python3 agy_send.py list [--cli] [--limit 20] [--json]
  python3 agy_send.py send <宛先> "<本文>" [--title 件名] [--sender 送り主] [--wait-idle 秒] [--force] [--cli] [--model 名前] [--cwd フォルダ] [--skip-permissions] [--json]
  python3 agy_send.py new "<本文>" [--title 件名] [--cli] [--model 名前] [--cwd フォルダ] [--skip-permissions] [--json]

既定はアプリの会話（agentapi）。--cli を付けると CLI の会話（agy）を扱う。両者は別管理。
宛先は会話ID（完全一致・先頭一致可）か会話タイトル。同名の会話がアプリと CLI の両方に
ある場合は、勝手に選ばず会話IDの指定を求めて止まる。
--title / --sender / --wait-idle / --force はアプリ経路のみ、--model はアプリの new または --cli 経路、--cwd / --skip-permissions は --cli 経路のみ指定できる。
アプリ経路の既定は SendUserCascadeMessage（画面ではユーザーの吹き出し）。相手が作業中でも割り込み送付する。
"""
import argparse
from collections import Counter, deque
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from relay_app_guard import ensure_app_running

APP_DB = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
ANTIGRAVITY_APP_BINARY = Path("/Applications/Antigravity.app/Contents/MacOS/Antigravity")
ANTIGRAVITY_APP_NAME = "Antigravity.app"
CLI_DB = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
APP_BIN = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi"
CLI_BIN = Path.home() / ".gemini" / "bin" / "agy"
DAEMON_DIR = Path.home() / ".gemini" / "antigravity" / "daemon"
BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
CLI_TIMEOUT_GRACE = 5
SNAPSHOT_RETRIES = 5
SNAPSHOT_RETRY_DELAY = 0.02
DEFAULT_WAIT_IDLE = 0.0
POLL_INTERVAL = 1.0
IDLE_STATUSES = {"", "IDLE", "CASCADE_RUN_STATUS_IDLE"}

LS_SERVICE = "exa.language_server_pb.LanguageServerService"
# 待機中: 新しいユーザー入力として返答を開始する
DELIVERY_WHEN_IDLE = "MESSAGE_DELIVERY_STRATEGY_WHEN_IDLE"
# 作業中: いまの実行に割り込んでユーザー入力として渡す（作業を止めずに受け取られる）
DELIVERY_WHEN_BUSY = "MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION"
MESSAGE_ORIGIN = "AGENT_MESSAGE_ORIGIN_SDK_EXECUTABLE"

class LanguageServerRpcUnavailable(Exception):
    """language_server が SendUserCascadeMessage を未実装と明示したときだけ使う。"""

class LanguageServerRpcAmbiguous(Exception):
    """届いたか分からない通信障害。フォールバックや再送はしない。"""

class LanguageServerNotSent(Exception):
    """送信前の段階で失敗し、まだ何も送っていないことが確かなとき。"""

# アプリ側 DB に混ざる CLI 会話の目印
CLI_SOURCE = "CORTEX_TRAJECTORY_SOURCE_CLI"
CLI_APP_DATA_DIR = "antigravity-cli"

# preview と raw_summary は本文を含むため読まない
QUERY = (
    "SELECT conversation_id, title, status, step_count, last_modified_time, "
    "source, app_data_dir FROM conversation_summaries "
    "ORDER BY last_modified_time DESC"
)

def find_bin(env_key, cmd_name, fallback_path):
    """実行ファイルを環境変数 → PATH → 標準位置の順で探す。"""
    cand = os.environ.get(env_key)
    if cand:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
        sys.exit(f"{env_key} の実行ファイルが見つかりません: {cand}")
    found = shutil.which(cmd_name)
    if found:
        return found
    if fallback_path.is_file() and os.access(fallback_path, os.X_OK):
        return str(fallback_path)
    sys.exit(
        f"{cmd_name} が見つかりません。{env_key} に実行ファイルのパスを指定してください"
        f"（標準位置: {fallback_path}）"
    )

def _file_sig(path):
    """複製前後の一貫性確認用に、ファイルの stat と先頭ヘッダ（無ければ None）を返す。"""
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            head = f.read(100)
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, head)

def _db_sigs(db_path):
    return tuple(_file_sig(Path(f"{db_path}{suffix}")) for suffix in ("", "-wal", "-journal"))

def _copy_snapshot(db_path, dst):
    for suffix in ("-wal", "-journal"):
        try:
            shutil.copyfile(Path(f"{db_path}{suffix}"), Path(f"{dst}{suffix}"))
        except FileNotFoundError:
            continue
    shutil.copyfile(db_path, dst)

def _query_temp_copy(db_path):
    """元ファイルに触れず、複製前後の署名一致を確認した上で WAL 等ごと一時複製して読む。"""
    for attempt in range(SNAPSHOT_RETRIES):
        with tempfile.TemporaryDirectory(prefix="agent-relay-agy-") as tmpdir:
            dst = Path(tmpdir) / db_path.name
            before = _db_sigs(db_path)
            _copy_snapshot(db_path, dst)
            after = _db_sigs(db_path)
            if before == after:
                return _query(str(dst), uri=False, check_integrity=True)
        if attempt + 1 < SNAPSHOT_RETRIES:
            time.sleep(SNAPSHOT_RETRY_DELAY)
    raise OSError("DB が更新中のため一貫した複製を取得できません")

def read_summaries(db_path):
    """会話一覧を読み取り専用で取る。存在しない DB は空扱い。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        return []
    try:
        return _query(f"file:{db_path}?mode=ro")
    except sqlite3.Error:
        try:
            return _query_temp_copy(db_path)
        except (sqlite3.Error, OSError) as e:
            sys.exit(f"会話一覧を読めません: {db_path}（{e}）")

def _query(target, uri=True, check_integrity=False):
    con = sqlite3.connect(target, uri=uri)
    try:
        if check_integrity:
            qc = con.execute("PRAGMA quick_check(1)").fetchone()
            if not qc or qc[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check 失敗: {qc[0] if qc else None}")
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(QUERY)]
    finally:
        con.close()

def _is_cli_row(row):
    """アプリ側 DB に混ざる CLI 会話行かどうかを返す。"""
    return row.get("source") == CLI_SOURCE or row.get("app_data_dir") == CLI_APP_DATA_DIR

def app_conversations():
    """アプリの会話。アプリ側 DB から CLI 会話を除いたもの。"""
    return [r for r in read_summaries(APP_DB) if not _is_cli_row(r)]

def cli_conversations():
    """CLI の会話。CLI 側 DB を優先し、アプリ側 DB の CLI 行を重複なく足す。"""
    rows = read_summaries(CLI_DB)
    seen = {r.get("conversation_id") for r in rows}
    for r in read_summaries(APP_DB):
        if _is_cli_row(r) and r.get("conversation_id") not in seen:
            rows.append(r)
            seen.add(r.get("conversation_id"))
    return rows

def match_rows(rows, target):
    """会話IDの完全一致を優先し、無ければタイトル一致と前方一致で探す。"""
    exact = [r for r in rows if r.get("conversation_id") == target]
    if exact:
        return exact
    return [
        r
        for r in rows
        if r.get("title") == target
        or str(r.get("conversation_id") or "").startswith(target)
    ]

def pick(rows, target):
    """宛先を1件に絞る。絞れなければ候補を出して終了する。"""
    hits = match_rows(rows, target)
    if len(hits) != 1:
        names = [f'{r.get("conversation_id")} {r.get("title")}' for r in hits] or ["該当なし"]
        sys.exit("宛先を1つに絞れません: " + " / ".join(names))
    return hits[0]

def cell(value):
    """TSV 用に1行へ均す。"""
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")

def emit_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))

def _pid_alive(pid):
    """生存中のプロセスなら True。EPERM は権限境界越しに存在するとみなす。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

def _daemon_files():
    if not DAEMON_DIR.is_dir():
        return []
    items = []
    for path in DAEMON_DIR.glob("*.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        items.append((mtime, path))
    items.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in items]

def _pid_comm(pid):
    """プロセスIDから実行ファイルパスを取得する。取得できない環境では None を返す。"""
    proc = None
    try:
        # テスト時に subprocess.run がモックされていても干渉しないよう Popen を使う
        proc = subprocess.Popen(
            ["ps", "-p", str(pid), "-o", "comm="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        stdout, _ = proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
            proc.communicate()
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    text = (stdout or "").strip()
    return text or None

def _is_non_app_hub_comm(comm):
    """Antigravity IDE や CLI が立てた agy プロセスなら True を返す。"""
    if not comm:
        return False
    name = Path(comm.strip()).name
    return name == "agy" or "Antigravity IDE.app" in comm

def _is_app_hub_comm(comm):
    """Antigravity.app 本体の language_server プロセスなら True を返す。"""
    if not comm or _is_non_app_hub_comm(comm):
        return False
    name = Path(comm.strip()).name
    return "Antigravity.app" in comm or name == "language_server"

def _agentapi_env():
    """agentapi が参照する接続先環境変数を返す。未設定時は daemon/*.json から本体アプリを優先して補う。"""
    address = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if address and token:
        return None
    if address or token:
        sys.exit("ANTIGRAVITY_LS_ADDRESS と ANTIGRAVITY_CSRF_TOKEN は両方指定するか、両方未設定にしてください")
    fallback_env = None
    skipped_non_app = False
    for path in _daemon_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not _pid_alive(pid):
            continue
        port = data.get("httpPort")
        csrf = data.get("csrfToken")
        if (
            isinstance(port, int)
            and not isinstance(port, bool)
            and port > 0
            and isinstance(csrf, str)
            and csrf
        ):
            comm = _pid_comm(pid)
            if _is_non_app_hub_comm(comm):
                # IDE 拡張や CLI の hub への誤爆を防ぐため除外する
                skipped_non_app = True
                continue
            env = os.environ.copy()
            env["ANTIGRAVITY_LS_ADDRESS"] = f"localhost:{port}"
            env["ANTIGRAVITY_CSRF_TOKEN"] = csrf
            if _is_app_hub_comm(comm):
                return env
            if fallback_env is None:
                fallback_env = env
    if fallback_env is not None:
        return fallback_env
    if skipped_non_app:
        sys.exit(
            "Antigravity デスクトップアプリ本体のプロセスが見つかりません"
            f"（IDE または CLI のプロセスのみ起動中: {DAEMON_DIR}）。"
            "Antigravity.app を開いてから再実行してください"
        )
    sys.exit(
        f"Antigravity の起動中プロセスが見つかりません（{DAEMON_DIR}）。"
        "アプリを開いてから再実行するか、ANTIGRAVITY_LS_ADDRESS と ANTIGRAVITY_CSRF_TOKEN を指定してください"
    )

def _resolve_ls_env():
    """接続用環境変数を返す。片方だけ設定されている場合は _agentapi_env が終了する。"""
    address = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if address and token:
        return os.environ.copy()
    env = _agentapi_env()
    if env is None:
        sys.exit("接続先を解決できませんでした")
    return env

def _ls_base_url(env):
    """環境変数から HTTP ベース URL と CSRF を取り出す。"""
    address = env.get("ANTIGRAVITY_LS_ADDRESS", "")
    token = env.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if not address or not token:
        sys.exit("接続先または CSRF トークンが未設定です")
    if address.startswith("http://") or address.startswith("https://"):
        base = address.rstrip("/")
    else:
        base = f"http://{address}"
    return base, token

def _rpc_unavailable_detail(status, detail):
    """HTTP 応答が RPC 未実装を示すとき True。"""
    blob = f"{status}\n{detail}".lower()
    needles = (
        "not found",
        "unimplemented",
        "unknown method",
        "no such method",
        "unrecognized",
    )
    if status in (404, 405, 501):
        return True
    return any(n in blob for n in needles)

def _language_server_rpc(env, method, body, timeout):
    """Connect 形式の JSON RPC を1回だけ投げる。"""
    base, csrf = _ls_base_url(env)
    url = f"{base}/{LS_SERVICE}/{method}"
    payload = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": csrf,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", errors="replace")[:800]
        if _rpc_unavailable_detail(e.code, detail):
            raise LanguageServerRpcUnavailable(f"{method} が HTTP {e.code}: {detail[:200]}")
        raise LanguageServerRpcAmbiguous(f"{method} が HTTP {e.code}: {detail[:200]}")
    except urllib.error.URLError as e:
        raise LanguageServerRpcAmbiguous(f"{method} への接続に失敗: {e}") from e
    except TimeoutError as e:
        raise LanguageServerRpcAmbiguous(f"{method} が {timeout} 秒以内に応答しませんでした") from e
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise LanguageServerRpcAmbiguous(f"{method} の応答が JSON ではありません") from e

def _last_user_turn_config(env, cascade_id, timeout):
    """直近のユーザー設定を流用する。plan model 未指定拒否を避ける。"""
    resp = _language_server_rpc(env, "GetCascadeTrajectory", {"cascadeId": cascade_id}, timeout)
    steps = (resp.get("trajectory") or {}).get("steps") or []
    for step in reversed(steps):
        user_input = step.get("userInput") or {}
        user_config = user_input.get("userConfig")
        if user_config:
            out = {"cascadeConfig": user_config}
            if user_input.get("userAgentSpec") is not None:
                out["customAgentSpec"] = user_input.get("userAgentSpec")
            return out
    return {}

def _delivery_strategy_for_status(status):
    if _is_busy_status(status):
        return DELIVERY_WHEN_BUSY
    return DELIVERY_WHEN_IDLE

def _format_outgoing_text(text, sender, message_id):
    """--sender 指定時は codex_send.py と同じ見出しを付ける。"""
    if not sender:
        return text
    sender = sender.strip()
    if not sender:
        return text
    return f"{sender}から\nメッセージ ID: {message_id}\n\n{text}"

def send_user_cascade_message(env, cascade_id, text, delivery_strategy, client_message_id, timeout):
    """SendUserCascadeMessage を1回送る。"""
    try:
        turn_config = _last_user_turn_config(env, cascade_id, timeout)
    except LanguageServerRpcAmbiguous as e:
        # 送信前の読み取りで失敗した場合は、まだ何も送っていない
        raise LanguageServerNotSent(f"送信前の設定の読み取りに失敗したため未送信です（{e}）") from e
    body = {
        "cascadeId": cascade_id,
        "items": [{"text": text}],
        "messageOrigin": MESSAGE_ORIGIN,
        "deliveryStrategy": delivery_strategy,
        "continueExecution": True,
        "clientMessageId": client_message_id,
        **turn_config,
    }
    return _language_server_rpc(env, "SendUserCascadeMessage", body, timeout)

def _send_via_agentapi_system_message(env, conversation_id, text, title, timeout):
    """従来経路: agentapi send-message（画面では System メッセージ）。"""
    cmd = [find_bin("ANTIGRAVITY_AGENTAPI", "agentapi", APP_BIN), "send-message"]
    if title:
        cmd += ["--title", title]
    cmd += [conversation_id, text]
    r = run(cmd, timeout, env=env)
    if r.returncode != 0:
        _fail_cmd("送信できませんでした", r)
    return r

def run(cmd, timeout, cwd=None, env=None):
    """外部コマンドを1回だけ実行する。自動再送はしない。"""
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        sys.exit(
            f"応答が {timeout} 秒以内に返りませんでした。"
            "相手側では処理が続いている可能性があります（自動再送はしません）"
        )
    except OSError as e:
        sys.exit(f"実行できません: {cmd[0]}（{e}）")

def tail(text, limit=500):
    text = (text or "").strip()
    return text[-limit:]

def _fail_cmd(prefix, result):
    detail = tail(result.stderr) or tail(result.stdout)
    message = f"{prefix}（終了コード {result.returncode}）: {detail}"
    output = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    if "operation not permitted" in output or "permission denied" in output:
        message += (
            '\nヒント: サンドボックスで localhost 接続が拒否されました。Codex 等では '
            'sandbox_permissions="require_escalated"（または sandbox_workspace_write.network_access=true）で再実行してください。'
        )
    sys.exit(message)

def resolve_timeout(a):
    timeout = a.timeout if a.timeout is not None else (300 if a.cli else 30)
    if timeout <= 0:
        sys.exit("--timeout は 1 以上の秒数を指定してください")
    return timeout

def load_json(text, label):
    try:
        return json.loads(text)
    except ValueError:
        sys.exit(f"{label}の JSON を解釈できません: {tail(text) or '（出力なし）'}")

def do_list(a):
    rows = cli_conversations() if a.cli else app_conversations()
    if a.limit > 0:
        rows = rows[: a.limit]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for r in rows:
        print(
            cell(r.get("status")),
            cell(r.get("step_count")),
            cell(r.get("last_modified_time")),
            cell(r.get("title")),
            cell(r.get("conversation_id")),
            sep="\t",
        )

def _candidate_lines(app_hits, cli_hits):
    """衝突候補を種別つきで並べる。"""
    lines = [f"[app] {r.get('conversation_id')} {r.get('title')}" for r in app_hits]
    lines += [f"[cli] {r.get('conversation_id')} {r.get('title')}" for r in cli_hits]
    return "\n".join(lines)

def _exit_collision(app_hits, cli_hits):
    """アプリと CLI の両方に候補があるときは勝手に選ばず止める。"""
    sys.exit(
        "アプリ会話と CLI 会話の両方に一致する候補があります。会話IDを指定してください:\n"
        + _candidate_lines(app_hits, cli_hits)
    )

def _exit_route_collision(use_cli, self_hits, other_hits):
    """呼び出し側の経路指定に関わらず [app] / [cli] の順で衝突候補を出して止める。"""
    app_hits, cli_hits = (other_hits, self_hits) if use_cli else (self_hits, other_hits)
    _exit_collision(app_hits, cli_hits)

def resolve_target(target, use_cli):
    """送信先を1件に絞る。アプリ/CLI の同名タイトル衝突は止める。"""
    app_rows = app_conversations()
    cli_rows = cli_conversations()
    selected, opposite = (cli_rows, app_rows) if use_cli else (app_rows, cli_rows)

    exact_self = [r for r in selected if r.get("conversation_id") == target]
    exact_other = [r for r in opposite if r.get("conversation_id") == target]
    if len(exact_self) == 1:
        # 会話IDの完全一致は明示指定として確定する
        if exact_other:
            _exit_route_collision(use_cli, exact_self, exact_other)
        return exact_self[0]

    self_hits = match_rows(selected, target)
    other_hits = match_rows(opposite, target)
    if self_hits and other_hits:
        _exit_route_collision(use_cli, self_hits, other_hits)

    if not self_hits and other_hits:
        if use_cli:
            sys.exit("アプリ会話には `--cli` を外して送信してください")
        sys.exit(
            "指定された宛先は CLI セッションです。`--cli` を付けて実行してください"
            "（`agentapi` は CLI セッションに届きません）"
        )

    return pick(selected, target)

def _run_agy_cli(a, timeout, conversation_id=None, failure_label="CLI への送信"):
    """agy CLI を実行し、完了まで待って JSON 応答を返す。"""
    cmd = [find_bin("ANTIGRAVITY_AGY", "agy", CLI_BIN), "-p", a.text]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    cmd += [
        "--output-format",
        "json",
        "--print-timeout",
        f"{timeout}s",
    ]
    if a.skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if a.model:
        cmd += ["--model", a.model]
    r = run(cmd, timeout + CLI_TIMEOUT_GRACE, cwd=a.cwd)
    if r.returncode != 0:
        _fail_cmd(f"{failure_label}に失敗しました", r)
    data = load_json(r.stdout, "agy")
    if a.json:
        emit_json(
            {"conversation_id": data.get("conversation_id"), "response": data.get("response")}
        )
    else:
        print("conversation_id:", cell(data.get("conversation_id")))
        print(data.get("response") or "")
    return data

def _is_busy_status(status):
    """会話が待機中以外の状態（作業中など）であれば True を返す。"""
    return (status or "").strip() not in IDLE_STATUSES

def _wait_until_sendable(conversation_id, initial_row, wait_seconds):
    """作業中の会話に対して最大 wait_seconds 秒だけ待機し、空かなければ止める。"""
    row = initial_row
    if not _is_busy_status(row.get("status")):
        return row
    elapsed = 0.0
    while elapsed < wait_seconds:
        step = min(POLL_INTERVAL, wait_seconds - elapsed)
        time.sleep(step)
        elapsed += step
        rows = read_summaries(APP_DB)
        matches = [r for r in rows if r.get("conversation_id") == conversation_id]
        if matches:
            row = matches[0]
            if not _is_busy_status(row.get("status")):
                return row
    sys.exit(
        f"宛先の会話が作業中（status={row.get('status')}）のため送信を見合わせました"
        f"（{wait_seconds:g}秒待機）。相手の返答が終わってから送り直すか、"
        f"急ぎの場合は --force を付けてください: {conversation_id}"
    )

def detect_branch_split(conversation_id, tail_lines=40):
    """会話ログから枝分かれ（非ユーザー入力ステップの多重重複や直近の巻き戻り重複）を検知する。"""
    logs_dir = BRAIN_DIR / conversation_id / ".system_generated" / "logs"
    target = None
    for name in ("transcript.jsonl", "transcript_full.jsonl"):
        cand = logs_dir / name
        if cand.is_file():
            target = cand
            break
    if target is None:
        return None
    non_user_counts = Counter()
    tail_items = deque(maxlen=tail_lines)
    try:
        with target.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                step = obj.get("step_index")
                if isinstance(step, int) and not isinstance(step, bool):
                    stype = obj.get("type") or ""
                    if stype != "USER_INPUT":
                        non_user_counts[step] += 1
                    tail_items.append((step, stype))
    except OSError:
        return None
    dup_non_user = sum(1 for _, cnt in non_user_counts.items() if cnt >= 2)
    if dup_non_user >= 6:
        return f"同一ステップ番号の重複が {dup_non_user} 箇所で発生"
    tail_non_user_counts = Counter(s for s, t in tail_items if t != "USER_INPUT")
    if tail_items:
        prev = tail_items[0][0]
        for curr, stype in list(tail_items)[1:]:
            if curr < prev and stype != "USER_INPUT" and tail_non_user_counts[curr] >= 2:
                return f"step {prev} -> {curr} の巻き戻り重複"
            prev = curr
    return None

def do_send(a):
    if a.cli and a.title:
        sys.exit("agy CLI では --title は指定できません")
    if not a.cli and (a.model or a.cwd or a.skip_permissions):
        sys.exit(
            "アプリ会話への send では --model / --cwd / --skip-permissions は指定できません"
            "（CLI 会話には --cli を付けてください）"
        )
    if a.wait_idle < 0:
        sys.exit("--wait-idle は 0 以上の秒数を指定してください")

    timeout = resolve_timeout(a)
    if not a.cli:
        ensure_app_running(ANTIGRAVITY_APP_BINARY, ANTIGRAVITY_APP_NAME)
    if a.cli:
        row = resolve_target(a.target, use_cli=True)
        _run_agy_cli(a, timeout, conversation_id=row["conversation_id"])
        return

    row = resolve_target(a.target, use_cli=False)
    if not a.force:
        split_reason = detect_branch_split(row["conversation_id"])
        if split_reason:
            sys.exit(
                f"宛先の会話ログで枝分かれ（{split_reason}）が検出されたため、"
                "暴走防止のため送信を止めました。新しい会話を立てて引き継ぐか、"
                f"強制送信する場合は --force を付けてください: {row['conversation_id']}"
            )
        if a.wait_idle > 0:
            row = _wait_until_sendable(row["conversation_id"], row, a.wait_idle)

    message_id = str(uuid.uuid4())
    payload_text = _format_outgoing_text(a.text, a.sender, message_id)
    env = _resolve_ls_env()
    delivery_strategy = _delivery_strategy_for_status(row.get("status"))
    route = "user_input"
    agentapi_result = None

    try:
        send_user_cascade_message(
            env,
            row["conversation_id"],
            payload_text,
            delivery_strategy,
            message_id,
            timeout,
        )
    except LanguageServerRpcUnavailable:
        route = "system_message"
        agentapi_result = _send_via_agentapi_system_message(
            env, row["conversation_id"], payload_text, a.title, timeout
        )
    except LanguageServerNotSent as e:
        print(f"{e}。メッセージ ID: {message_id}", file=sys.stderr)
        sys.exit(3)
    except LanguageServerRpcAmbiguous as e:
        sys.exit(
            f"送信結果が不明です（{e}）。自動再送や経路切り替えはしません。"
            f" メッセージ ID: {message_id}"
        )

    if a.json:
        data = {
            "conversation_id": row["conversation_id"],
            "title": row.get("title"),
            "accepted": True,
            "route": route,
            "message_id": message_id,
            "delivery_strategy": delivery_strategy if route == "user_input" else None,
        }
        if agentapi_result is not None:
            data["returncode"] = agentapi_result.returncode
            data["stdout"] = agentapi_result.stdout.strip()
            data["stderr"] = agentapi_result.stderr.strip()
        emit_json(data)
    else:
        print(
            "送信受付:",
            row["conversation_id"],
            cell(row.get("title")),
            f"経路={route}",
            f"message_id={message_id}",
        )

def do_new(a):
    if a.cli and a.title:
        sys.exit("agy CLI では --title は指定できません")
    if not a.cli and (a.cwd or a.skip_permissions):
        sys.exit(
            "アプリ会話の new では --cwd / --skip-permissions は指定できません"
            "（CLI で作るには --cli を付けてください）"
        )

    timeout = resolve_timeout(a)
    if a.cli:
        _run_agy_cli(a, timeout, failure_label="CLI での新規作成")
        return

    env = _agentapi_env()
    cmd = [find_bin("ANTIGRAVITY_AGENTAPI", "agentapi", APP_BIN), "new-conversation"]
    if a.model:
        cmd += ["--model", a.model]
    if a.title:
        cmd += ["--title", a.title]
    cmd.append(a.text)
    r = run(cmd, timeout, env=env)
    if r.returncode != 0:
        _fail_cmd("新規会話を作れませんでした", r)
    data = load_json(r.stdout, "agentapi")
    conv_id = new_conversation_id(data)
    if not conv_id:
        sys.exit(f"新規会話IDを取得できません: {tail(r.stdout)}")
    if a.json:
        emit_json({"conversation_id": conv_id})
    else:
        print("conversationId:", conv_id)

def new_conversation_id(payload):
    """応答から新規会話IDを取る。キー名の綴り違い（newConversationn）が実在する。"""
    node = payload.get("response") if isinstance(payload, dict) else None
    if not isinstance(node, dict):
        return None
    for key in ("newConversationn", "newConversation"):
        item = node.get(key)
        if isinstance(item, dict) and item.get("conversationId"):
            return item["conversationId"]
    return None

def build_parser():
    p = argparse.ArgumentParser(description="Antigravity の会話一覧・送信・新規作成")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="会話一覧")
    lp.add_argument("--cli", action="store_true", help="CLI 会話を表示する")
    lp.add_argument("--limit", type=int, default=20, help="件数（0 以下で全件）")
    lp.add_argument("--json", action="store_true", help="JSON で出力する")
    lp.set_defaults(func=do_list)

    sp = sub.add_parser("send", help="既存の会話へ送る")
    sp.add_argument("target", help="会話IDか会話タイトル")
    sp.add_argument("text", help="本文")
    sp.add_argument("--title", help="通知タイトル（system_message フォールバック時のみ）")
    sp.add_argument(
        "--sender",
        help="本文先頭に「<送り主>から」「メッセージ ID: …」の見出しを付ける",
    )
    sp.add_argument(
        "--wait-idle",
        type=float,
        default=DEFAULT_WAIT_IDLE,
        help="相手が作業中のときに待機する最大秒数（0 で待機なし、既定 0）",
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="作業中ストッパーおよび会話分岐ストッパーを無視して強制送信する",
    )
    sp.add_argument("--cli", action="store_true", help="CLI 会話へ送る")
    sp.add_argument("--model", help="--cli 時のモデル")
    sp.add_argument("--cwd", help="--cli 時の作業フォルダ")
    sp.add_argument("--skip-permissions", action="store_true", help="--cli 時に権限確認を省略する")
    sp.add_argument("--timeout", type=int, help="待ち時間の秒（既定はアプリ30 / CLI300）")
    sp.add_argument("--json", action="store_true", help="JSON で出力する")
    sp.set_defaults(func=do_send)

    np = sub.add_parser("new", help="新しい会話を作る")
    np.add_argument("text", help="最初の本文")
    np.add_argument("--title", help="会話タイトル")
    np.add_argument("--model", help="モデル")
    np.add_argument("--cli", action="store_true", help="CLI で作る")
    np.add_argument("--cwd", help="--cli 時の作業フォルダ")
    np.add_argument("--skip-permissions", action="store_true", help="--cli 時に権限確認を省略する")
    np.add_argument("--timeout", type=int, help="待ち時間の秒（既定はアプリ30 / CLI300）")
    np.add_argument("--json", action="store_true", help="JSON で出力する")
    np.set_defaults(func=do_new)

    return p

def main(argv=None):
    a = build_parser().parse_args(argv)
    a.func(a)

if __name__ == "__main__":
    main()
