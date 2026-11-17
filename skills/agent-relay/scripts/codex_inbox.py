#!/usr/bin/env python3
"""専任子へのファイル通知用。通常の Desktop への送信は codex_send.py を使う。"""
import argparse
import json
import os
from pathlib import Path
import select
import sys
import uuid

def send(folder):
    body = sys.stdin.read()
    if not body:
        raise SystemExit("本文が空です")
    name = uuid.uuid4().hex
    temporary = folder / (name + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(body)
        temporary.rename(folder / (name + ".msg"))
    finally:
        temporary.unlink(missing_ok=True)
    print(name)

def watch(folder):
    if not hasattr(select, "kqueue"):
        raise SystemExit("受信待ちは macOS の kqueue が必要です")
    seen = set()
    fd = os.open(folder, os.O_RDONLY)
    try:
        queue = select.kqueue()
        try:
            event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                  fflags=select.KQ_NOTE_WRITE)
            queue.control([event], 0)
            while True:
                for path in sorted(folder.glob("*.msg")):
                    if path.name in seen:
                        continue
                    try:
                        body = path.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        continue
                    print(json.dumps({"id": path.name, "body": body}, ensure_ascii=False), flush=True)
                    seen.add(path.name)
                queue.control(None, 1)
        finally:
            queue.close()
    finally:
        os.close(fd)

def main():
    parser = argparse.ArgumentParser(description="Codex 受信箱の送信とイベント待ち")
    parser.add_argument("action", choices=("send", "watch"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.action == "send":
        send(args.folder)
    else:
        watch(args.folder)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
