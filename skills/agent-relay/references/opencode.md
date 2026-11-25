# OpenCode への送信

## 既存セッションへ送る

```bash
opencode api session.list
python3 scripts/opencode_send.py "$session_id" --sender "$sender_name" --message-id "$message_id" < "$body_file"
opencode api session.message.list --param "sessionID=$session_id"
```

直接短い本文を渡す場合は、送信コマンドを `python3 scripts/opencode_send.py "$session_id" "本文"` と書くこともできる。

`session.list` はセッションを新しい順に表示する。既存セッションに送るには OpenCode.app が起動している必要がある。ウィンドウを閉じてもプロセスが残っていれば送信できる。常駐 CLI だけが動いている場合は、終了コード `3` で止まる。API の呼び出しには `opencode api` を使う。

画面で開いていないセッションにも送れる。2026-11-17 の試験では、8 時間前に止まっていたセッションに送った文が応答の `data` に同じ ID で記録され、その後に返事が付いた。作業中の取り込み時点は未確認。応答に `"delivery":"steer"` が付くことだけでは、現在のターンで読まれたかは判断できない。

## 受付・到達・返事を確認する

`--sender` を付けると本文に送り主とメッセージ ID が入る。結果の JSON には `status`、`arrival`、`message_id`、`message_object_id`、API 応答が入る。`arrival: confirmed` は応答にメッセージオブジェクトの ID がある場合にも返る。宛先に本文が届いたかは、応答内のユーザー入力か会話記録で本文または同じメッセージ ID を照合する。

`session.prompt` は返事を待たずに戻る。`session.message.list` の末尾が `{"type":"idle","outcome":"succeeded"}` になれば、そのセッションの処理は終了している。依頼に沿った返事や成果も確認する。

`SessionNotFoundError` で終了コード `3` が出た場合は宛先を確認する。通信の待ち時間切れは結果不明（終了コード `1`）。同じ宛先とメッセージ ID の `accepted` が残っていれば再送を止める。`unknown` は記録に残る。再試行は会話記録で未着を確認してから行う。[到達と再送の共通手順](delivery.md)

## CLI で新規作成・追記する

```bash
opencode run --title "$session_name" --format json "最初の指示" < /dev/null
opencode run -s "$session_id" --format json "本文" < /dev/null
```

## 確認できた範囲

OpenCode.app 2.2.7 と CLI 2.2.5 の組み合わせでは、Desktop で開いている会話への送信と、そこから Antigravity への返信を確認した（2026-11-14）。macOS でウィンドウがない状態でも、送信とユーザー入力への到達を確認した。

アプリが 1.x、CLI が 2.x の環境では、保存先の違いで `SessionNotFoundError` が起きた。Homebrew で導入したアプリを 2.x に揃えるときは `brew upgrade --cask opencode-desktop` を使う。CLI で作ったセッションが Desktop 2.x の一覧に出るかは未確認。
