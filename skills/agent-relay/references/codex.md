# Codex への送信

## Codex のチャットとホスト

Codex Desktop の一覧には、この端末で動くチャット（local）と別のホストで動くチャット（durable）が並ぶ。チャット名と ID（`threadId`）を照合し、確認できる場合はホスト（`hostId`）も控える。

`codex_send.py` が送れるのはローカルチャット。durable の宛先を指定されたらローカルに読み替えない。`--host-id durable` は送信前に終了コード `3` で止まる。

## ローカルの通常送信は Desktop の公式キュー

```bash
python3 scripts/codex_send.py --host-id local --thread "$thread_id" \
  --sender "$sender_name" --message-id "$message_id" < "$body_file"
```

「このチャットに送って」という依頼なら、送信元を問わず上のコマンドを使う。宛先が返答中でも、そのまま公式キューに入る。`--visible` は従来の書き方との互換用で、結果は既定と同じ。

スクリプトは Desktop の起動と、指定したローカルチャットの記録を確認する。待機中なら、条件が揃うと新しいターンが始まる。作業中は通常、今のターンが終わるまで待つ。キューから会話本文へ取り込まれると、送り主を添えたユーザー発言として表示される。

実機では、Antigravity から続けて送った2通が Codex のキューに入り、ユーザーが「今すぐ反映」を押すと両方を会話中に読めた。自動で割り込んだ例には数えない。

ユーザーが直前の作業を中断した場合には、キューも一時停止することがある。そのままにするか、「再開」を押して続けるかはユーザーが決める。

チャットが読み込まれていないと、キューに入っても送信先で止まる場合がある。画面を切り替えてよい場合は、対象のチャットを Desktop で開く。未読み込みだったチャットをアプリで開くと、キューの入力で新しいターンが始まった例が2件ある。ウィンドウを閉じているときや利用上限から回復した後も自動で始まるかは未確認。

Desktop の「Follow-up behavior」と VS Code の `chatgpt.followUpQueueMode` は、それぞれの入力欄から作業中に送った文の設定。外からの `codex queue` や追記ツールの送り方が変わるかは確認できていない。[Desktop 設定](https://learn.chatgpt.com/docs/app/settings#general)・[VS Code 拡張の設定](https://learn.chatgpt.com/docs/developer-settings#editor-settings-reference)

CLI に直接渡す場合は、宛先 ID と送り主・メッセージ ID 入りの本文を指定する。

```bash
codex queue --thread "$thread_id" --message "$message_with_sender_and_id"
```

`codex queue` の `--resume` は、投入後にローカル CLI デーモンか指定のリモートサーバーで再開を試みる。再開に失敗しても警告だけの場合がある。Desktop で未読み込みのチャットを開くかは未確認で、`codex_send.py` ではこのオプションを使わない。[Codex CLI のキュー処理](https://github.com/openai/codex/blob/main/codex-rs/tui/src/session_queue_commands.rs)

## 公式 App Server を明示して既存チャットへ送る

キューを使えない場合や同じターンに届ける必要がある場合は、対象チャットを読み込んでいる公式 App Server の Unix ソケットを使えるか確認する。利用者が接続を承認したソケットを指定する。`hostId=local`、表示上の `active`、ローカル記録から分かるのは所在と状態まで。`--listen` は App Server の起動時に指定する。稼働中の Desktop に公式リスナーを追加する手順は確認できていない。既定の CLI デーモンや新規の App Server は、対象 Desktop のチャットを扱っていると確認できなければ使わない。[App Server の接続仕様](https://learn.chatgpt.com/docs/app-server#protocol)

接続先を毎回指定するなら `--app-server-socket PATH` を使う。同じソケットを繰り返し使うなら、送信するシェルで `CODEX_APP_SERVER_SOCKET` を環境変数として設定し、実行時に `--app-server` を付ける。環境変数を設定しただけでは通常どおりキューに送る。

`--expected-turn-id` を使う場合も、ソケットは引数か環境変数で指定する。`--visible` と `--steer-only` はどちらか一方だけ使える。どちらも App Server の指定や `--expected-turn-id` との併用はできない。

```bash
uv run --with 'websockets>=11' python scripts/codex_send.py \
  --app-server --host-id local --thread "$thread_id" --sender "$sender_name" --message-id "$message_id" < "$body_file"
```

`websockets` 11 以降が既にあれば `python3` で実行できる。スクリプトは本文を読み込む前と後に、`thread/read` で宛先と状態を照合する。実行中なら `thread/turns/list` で最新の実行中ターンを取り、`expectedTurnId` 付きで `turn/steer` を一度呼ぶ。待機中の `idle` は読み込み済みの会話を指す。この状態なら同じチャットに `turn/start` でユーザー入力を一度送る。未読み込みや状態不明、照会エラーの場合は未送信で止まる。

待機状態の照合直後に別の入力でターンが始まると、App Server はそのターンへの追記として受理することがある。`route: app_server_start` は呼び出した API を示す。新しいターンが始まったかは、返った `turn_id` と `turn/started` などの記録を照合する。[指定ターンへの追記](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn)・[競合時の公式テスト](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/app-server/tests/suite/v2/turn_start.rs#L654)

### 実行中の指定ターンに限る場合

`--expected-turn-id` に対象ターン ID を渡す。公式 Unix ソケットは環境変数か引数で指定する。

追記するには、実行中のターンを一覧から特定できる必要がある。App Server 0.172.0 では一時チャット（ephemeral）のターン一覧を取得できないため、返答中の一時チャットには送れない。読み込み済みで待機中なら新しいターンを開始できる。[App Server の実装](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/app-server/src/request_processors/thread_processor.rs#L3987-L4001)

```bash
uv run --with 'websockets>=11' python scripts/codex_send.py \
  --app-server-socket "$app_server_socket" --host-id local --thread "$thread_id" \
  --expected-turn-id "$turn_id" --sender "$sender_name" --message-id "$message_id" < "$body_file"
```

待機中やターン不一致では未送信。照合後にターンが変わっても App Server が `expectedTurnId` の不一致で拒否する。接続先や対象を照合できない場合は未送信（`3`）。ソケットの指定がなければ引数エラー（`2`）。送信後に応答が欠けたら結果不明（`1`）。この経路の選択時は、別経路へ切り替えず、対象の画面や状態を変えずに結果を返す。

`status: accepted` は App Server の受付。`arrival` と `read` は `unconfirmed` で返る。受信記録のターン ID とメッセージ ID、ターン終了前に内容に沿った応答や処理があったかを照合する。通常のユーザー入力と同じ表示も必要なら、受信側の画面で吹き出しを確認する。

## Codex のネイティブツール

`send_message_to_thread` を利用できる場合は、ユーザーの明示許可を得たうえで、ツールが指定する宛先と関係の条件に従う。ツールが宛先条件や権限を理由に拒否した操作は、別経路で迂回しない。

2026-11-26 の Desktop では、実行中のチャットに `function_call_output` として入り、宛先が同じターンで内容を復唱した。このツールにはターン ID を指定する引数がない。受信画面にユーザー入力の吹き出しとして出るかは未確認。

## Desktop の従来の割り込み機能

`--steer-only` は従来の差し込み専用オプションとして互換を維持する。実行中のターン ID は指定できず、待機中や受信先不在の明示的な拒否では未送信（`3`）。`--no-wake` は互換のため受理され、どのモードでも画面を自動で開かない。

送信前の `Connection refused` など接続・初期化の失敗では本文を渡さず止まる（`3`）。本文を渡した後の通信断などは結果不明（`1`）で止まり、キューへの切替は行わない。`status` は差し込み受付で `accepted`、通常のキュー投入で `queued`。`route` が経路、`record` が照合した会話記録の場所。`arrival: confirmed` は記録末尾に同じメッセージ ID があったことを示す。受信結果が分からなければ [到達と再送](delivery.md) を参照する。

### 実測と制約

- 2026-11-16 に Codex から Codex Desktop の作業中のターンへ差し込んだ。受信記録にユーザー入力が入り、ユーザーが画面の吹き出しを確認した。同日の Antigravity からの 2 通はキュー経由だった。Claude Code、OpenCode、Cursor、Grok Bot、Muse からも到達例がある。
- 2026-11-17 は `sandbox: managed`・`approval: on-request` の Desktop 2件で、キューから手動の受信承認を挟まずにターンが始まった。この組み合わせの差し込みは未検証。受信後の操作には宛先の権限が適用される。
- VS Code を閉じた後に既存連携の受信先が見つからなくなり、Desktop の再起動後に回復した例がある。接続拒否とは別の結果なので、現行環境の原因を確かめてから復旧方法を決める。
- 旧方式の Desktop `start` は使わない。送信後に画面からの操作が active turn ID 不明で止まり、再起動後に回復した例がある。原因は未確定。
- 専任の子がファイルを監視して親に知らせる方式は、親のターンを自動で起こさない。[旧方式の検証記録](codex-busy-inbox.md)

## 新規チャットと CLI

Desktop に新しいチャットを作る依頼では、利用可能なら `create_thread` を使う。

CLI 内で完結する新規・追記には共有デーモンを使える。このデーモンで作ったセッションは Desktop の一覧には出なかった。

```bash
python3 scripts/codex_daemon.py new "最初の指示" --name "$thread_name" --cwd "$project_dir"
python3 scripts/codex_daemon.py send "$thread_id" "本文"
```

返事はそのまま表示される。別案として `codex exec --json` で新規、`codex exec --json resume "$thread_id" "本文"` で追記できる。新規の ID は `thread.started` の `thread_id`。Desktop での表示は未確認。CLI のセッションに `codex queue` を使う場合も、アプリかデーモンが対象を読み込んでいる必要がある。`codex exec` で作ったセッションはキューを積むだけでは動かなかった。
