# Cursor・Grok Bot・Muse・dots

## Cursor

Cursor Desktop の会話からは、`codex_send.py` などを使って他のアプリへ送れる。相手の会話記録で到達や返事を確認する。外部から Cursor Desktop の既存会話に返信を入れたり、待機中の会話を起こしたりする手段は未確認。

Cursor CLI では新規作成と追記ができる。

```bash
cursor-agent -p --mode plan --trust --model "$model" --output-format json "最初の指示" < /dev/null
cursor-agent -p --mode plan --trust --model "$model" --resume "$chat_id" "本文" < /dev/null
```

チャット ID は結果の JSON にある `session_id`。空のチャットだけ作る場合は `cursor-agent create-chat` を使う。`composer-2.5-fast` の `--resume` で返事の本文が空になった例がある。該当する環境での追記には `composer-2.5` を使う。`cursor-agent ls` は対話画面向けで、スクリプトから呼ぶと止まったまま残ることがある。

Desktop の Agents 画面にある「This Mac」の会話 ID を `--resume` に渡すと、同じ ID の別の CLI チャットができ、アプリの文脈を引き継がなかった（2026-11-02）。同日の Cloud Agents API の一覧では、調べた先頭 20 件に Cloud と My Machines のエージェントだけが表示された。Cloud Agents API を介した追記と画面への表示は未確認。

## Grok Bot（xAI）

### Routine を準備する

Grok Bot を受け手にする場合は、Webhook であらかじめ設定した Routine を起動する。既存の会話へユーザー入力として書き込む経路は確認できていない。

Routine は Webhook の `request` があれば、それを依頼本文として扱う。旧方式にも対応するなら、`request` がないときにだけ Mac の `~/Library/Application Support/agent-relay/inbox/grokbot.md` を読む。送り主には `from` を使い、宛先や返信先が本文に書いてあればその指定に従う。Mac Shell から送信スクリプトを呼ぶときは、インストール先のスクリプトを絶対パスで指定する。[Routine の設定](https://cursor.com/help/grok-bot/routines)

Webhook の URL とキーは、権限のある利用者がアプリの「詳細」から該当ルーティンを開いて確認する。Mac では同梱スクリプトがキーチェーンの `grokbot-webhook-url` と `grokbot-webhook-key`（アカウント名は共に `agent-relay`）を優先する。再発行時は対応する項目を更新する。URL やキーは会話に貼らない。

### 送信する

```bash
python3 scripts/grokbot_send.py send "依頼本文" --from "$sender_name"
```

`--from` を省くと送り主は `unknown`。Webhook には `from`、`request`、Routine 向けの説明 `task` が入る。Mac のキーチェーン経路は、従来の互換用ファイルにも本文を保存する。キーチェーンが使えない環境で `GROKBOT_WEBHOOK_URL` と `GROKBOT_WEBHOOK_KEY` を設定した場合は、Webhook の本文だけを送る。互換用ファイルは書かない。

Mac のキーチェーン経路では、送信前に Grok Bot.app の起動と Mac への接続を確認する。アプリが閉じているか、ログに未接続と出ている場合は終了コード `3`。ログが読めない場合は警告を出して送る。`--force` はこの確認を省く指定。環境変数経路では Mac 側の起動・接続を確認しない。互換用ファイルは次の送信で上書きされるため、それを読む Routine では前の依頼が読まれたことを確かめてから続ける。

### 到達と返信を確認する

HTTP 200 と `runUuid` は Routine の受付を示す。受付後に Mac Shell が動かない場合もある。依頼に沿った投稿や返信があるか、Routine を設定したチャットまたは指定した返信先で確認する。画面に出るのは Webhook の内容や Routine の実行結果。ユーザー発言の吹き出しが必要な場合には、この表示だけで条件を満たしたとは判断できない。

Grok Bot から他のアプリへ送るときは、Mac Shell に宛先の Mac を指定する。指定しないシェルはクラウド側で実行される。2026年11月2日に Claude Code との往復を、11月14日に Antigravity への返信を確認した。Mac がスリープしているときの動作と、Routine が毎回どこまで以前の会話を使うかは未確認。

## Muse

Muse.app のメイン会話に送るには、Muse Gadget として登録したクライアントと、Mac 上で常駐するブリッジが必要。ブリッジはこのスキルに同梱していない。設定はブリッジの README に従う。

```bash
python3 scripts/muse_send.py "本文" --sender "$sender_name" --subject "$subject" --reply "$reply_to"
```

本文を標準入力から渡す場合は、引数の「本文」を省き、同じコマンドにファイルの内容を渡す。受付フォルダは `MUSE_BRIDGE_INBOX` で指定する。Muse.app が閉じていれば終了コード `3`、受付フォルダが見つからなければ終了コード `1`。送信スクリプトは、送り主、件名、本文、返信方法が入った Markdown を一時名で書き、`.md` にリネームする。標準出力に出るファイル名で確認できるのは、受付フォルダへの配置まで。

スクリプトでブリッジの受信用フォルダを使うには、ブリッジの README で指定された `messages/inbox/` を `MUSE_BRIDGE_INBOX` に設定する。

### 到達と返信を確認する

Muse のメイン会話に送った文が表示されれば、到達を確認できる。その内容に沿った返事や処理があれば、Muse が読んだことも分かる。2026-11-14 に、Antigravity から `messages/inbox/` 経由で Muse への配送と、Muse から `cli/inbox` の `agy_send` ジョブを使った返信を確認した。ジョブの書式はブリッジの README を参照する。

Codex へ返信するには、ブリッジの `codex_send` ジョブを使う。実行中のターンに届ける条件は [Codex の送信方法](codex.md) を参照する。2026-11-11 には、ユーザーが委任を伝え、参照ファイルに代えて本文を渡す方式で Codex と Muse の往復を確認した。受信側の権限や承認は引き続き適用される。

### Muse Code

Muse Code はコーディングエージェントの CLI。対話セッション同士で公式のセッション間メッセージを使える。受け手の承認が必要で、期限は 30 分。別プロセスから外部送信できるかは未確認。

## dots

ChatGPT の dots に外から文章を入れる経路は未確認。自前の MCP サーバーから MCP Events を使う案も、会話に表示されるところまでは確認できていない。Codex の durable チャットについては、[Codex のチャットとホスト](codex.md#codex-のチャットとホスト)を参照する。
