"""起動中の Claude Code セッションへ、外部プロセスからメッセージを1通送る。

使い方:
  python3 claude_send.py list
  python3 claude_send.py send <宛先> "<本文>"
宛先はセッション名、PID、sessionId（先頭一致可）のどれか。
"""
import glob
import json
import os
import socket
import sys

SESSIONS = os.path.expanduser("~/.claude/sessions")

def sessions():
    out = []
    for p in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sock = d.get("messagingSocketPath")
        # 終了済みセッションの残骸は除く
        if sock and os.path.exists(sock):
            out.append(d)
    return out

def find(target):
    hits = [d for d in sessions()
            if target in (d.get("name"), str(d.get("pid")))
            or str(d.get("sessionId", "")).startswith(target)]
    if len(hits) != 1:
        names = [f'{d.get("pid")} {d.get("name")}' for d in hits] or ["該当なし"]
        sys.exit("宛先を1つに絞れません: " + " / ".join(names))
    return hits[0]

def send(sock_path, text):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sock_path)
    # 1行のJSONを送って送信側を閉じる。応答は返らない（空）のが正常
    frame = {"type": "user", "message": {"role": "user", "content": text}}
    s.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode())
    s.shutdown(socket.SHUT_WR)
    s.close()

if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        for d in sessions():
            print(d.get("pid"), d.get("status"), d.get("kind"), d.get("name"), d.get("sessionId"), sep="\t")
    elif len(sys.argv) == 4 and sys.argv[1] == "send":
        d = find(sys.argv[2])
        send(d["messagingSocketPath"], sys.argv[3])
        print("送信:", d.get("pid"), d.get("name"), d.get("sessionId"))
    else:
        sys.exit(__doc__)
