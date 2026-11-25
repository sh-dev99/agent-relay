# Antigravity への送信

## アプリと CLI のどちらに送るか

アプリと CLI は、会話の一覧も送り方も別。アプリ会話が既定で、CLI 会話を扱う場合は `--cli` を付ける。同じ名前が両方にあるときは `[app]` と `[cli]` の候補が出るので、該当する会話 ID を指定する。違う種類の会話 ID を渡すと、送信せずに止まる。

```bash
python3 scripts/agy_send.py list
python3 scripts/agy_send.py list --cli
python3 scripts/agy_send.py send "$conversation_id" "本文" --sender "$sender_name"
python3 scripts/agy_send.py send "$conversation_id" "本文" --cli --model "$model" --cwd "$project_dir"
python3 scripts/agy_send.py new "最初の指示" --title "$conversation_title"
python3 scripts/agy_send.py new "最初の指示" --cli --model "$model" --cwd "$project_dir"
```

## アプリで使っている会話へ送る

既存のアプリ会話には `agy_send.py send` で送る。ユーザー入力の経路では、取り込まれた文章がユーザーの吹き出しに表示される。会話記録では `USER_INPUT` / `USER_EXPLICIT` として残る。待機中なら新しい返事が始まる。

作業中の会話へ送ると、相手が次にモデルを呼び出す際に新しい文を渡す（`MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION`）。2026-11-25 に Antigravity.app 2.35.1 でこの動きを確認した。翌日の連続送信でも、AGY は1通目でタイマーを設定し、その待ち時間に2通目を読んで先に返信した。このとき AGY は子エージェントを使っていなかった。

公式の変更履歴によると、親のターンが終わっても、子エージェントやバックグラウンドの作業が続いていると配信が保留されることがある。保留の理由はキュー欄で確認でき、画面の `Send Now` で手動反映できる。`agentapi send-message` のヘルプでは、同じ操作をするオプションを確認できなかった。過去に保留された個別のメッセージがどの条件に当たったかは未確認。[Antigravity App 2.35.0 の変更履歴](https://antigravity.google/docs/changelog?tab=hub)

既定では宛先が待機状態になるまで待たずに送信する。待機中にだけ送りたい場合は `--wait-idle <秒>` を使う。指定時間内に待機状態へ移らなければ送信を見合わせる。宛先の会話記録から枝分かれを検出した場合も止まる。`--force` は枝分かれの検出と待機を省く指定。配信時点は変えない。

`--sender` を付けると本文に送り主とメッセージ ID を付ける。コマンドの結果に表示される `route` とメッセージ ID を控え、到達は会話記録や画面で確かめる。宛先で読んだことは返事や処理で確認する。別の AI からの返信を待つためだけにコマンドを動かし続ける必要はない。用件が済んでいれば、ターンを終えて待てる。保留されている配信はキュー欄で確認する。[確認と再送の共通手順](delivery.md)

## 起動と接続を確かめる

アプリ会話への `send` では Antigravity.app の起動を確認する。接続先の自動選択では、アプリ本体の `language_server` を優先し、IDE・CLI と識別できた接続先を除外する。アプリが起動していなければ終了コード `3`。赤ボタンでウィンドウを閉じた際にアプリと `language_server` が終了した例がある（macOS 26、Antigravity 2.32.1）。その場合はアプリを開いてから改めて送る。`new` と `--cli` は、この起動確認の対象外。

宛先の一覧には、アプリの `~/.gemini/antigravity/conversation_summaries.db` と CLI の `~/.gemini/antigravity-cli/conversation_summaries.db` を読み取り専用で使う。アプリ側のデータベースに記録されていても、CLI で作られた会話は CLI の一覧に表示する。Grok Bot の Mac Shell や Muse のブリッジから送る場合も、明示的な接続設定がなければ既存の接続先を自動で選ぶ。接続設定が不完全な場合は止まる。[送信元の権限](delivery.md#送信元の権限)

## システム通知と公式 sidecar

アプリへの送信では `user_input` が既定。ユーザー入力の機能が未実装などと明示された場合に限り、`agentapi send-message` に切り替わる。この場合は `system_message` として受け付けられ、画面では「Message from System」に表示される。接続が途中で切れるなど送信結果が分からない場合は、再送や経路の切り替えの前に到達を確かめる。

Google が公開している sidecar では、グローバル設定で有効化した sidecar 内から `agentapi send-message` で既存の会話 ID を指定できる。設定変更が稼働中のアプリに反映される時点、ユーザー入力としての表示、指定した実行中ターンでの取り込みは、公開仕様からは確認できていない。[Antigravity Sidecars](https://antigravity.google/docs/sidecars#agentapi)

## CLI と新規作成

CLI 会話の送信には `agy -p --conversation` を使い、応答が返るまで待つ。アプリの新規会話は `agentapi new-conversation` で作る。実行ファイルは `ANTIGRAVITY_AGENTAPI` / `ANTIGRAVITY_AGY`、`PATH`、既定の場所（`~/.gemini/antigravity/bin/agentapi`、`~/.gemini/bin/agy`）の順に探す。

- アプリの `send` だけで使う設定は `--sender`、`--wait-idle`、`--force`。`--title` はシステム通知へ切り替わった場合の件名。
- `--model` はアプリの `new` と CLI 経路で使う。`--cwd` と `--skip-permissions` は CLI 経路専用。権限確認は既定で有効。
- `--json` で結果を JSON にできる。`--timeout` の単位は秒で、既定はアプリが 30 秒、CLI が 300 秒。

2026-11-14 に、Antigravity と Claude Code、Codex、OpenCode、Grok Bot、Muse の間で、それぞれ往復を確認した。Cursor は Antigravity への片道を確認した。
