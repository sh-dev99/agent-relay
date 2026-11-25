# Claude Code への送信

## Claude Code 同士で送る

`ListAgents` で相手の名前を確認し、`SendMessage` で送る。`notify_when_idle: true` を付けると、相手のターンが終わったあとに返事の要約が通知される。作業中の Claude Code が合間に受け取った例と、Desktop でターン終了まで保留された例がある。[Claude Code のセッション間メッセージ](https://code.claude.com/docs/en/cross-session-messaging)

## 他のアプリやシェルから送る

```bash
python3 scripts/claude_send.py list
python3 scripts/claude_send.py send "$session_id" --sender "$sender_name" --message-id "$message_id" < "$body_file"
```

`list` には受信用ソケットのパスが確認できるセッションが出る。`send` では、一覧に出ない会話も探せる。プロセス ID（PID）かセッション ID（sessionId）を指定できる。セッション ID の先頭部分でも検索できる。

名前で指定すると、`~/.claude/sessions/*.json` の名前と会話記録の表示名（`custom-title`）を照合する。同じ sessionId の重複行は一つの会話として扱う。sessionId が異なる同名の候補があれば、起動中かどうかで選ばず、候補を表示して止まる。会話記録を読めなかった場合も名前での送信を見合わせる。候補の sessionId を指定し直す。

古い sessionId で指定したときは、セッション情報やアプリに残った対応記録から同じ会話と確認できる起動中の候補を探す。見つかれば送信先を標準エラー出力に表示する。この対応づけは記録に基づく。アプリの再起動後も同じ対応が残るかは未確認。

### 会話がアプリで開いていない場合

受信用ソケットが使えれば、そのまま送る。会話記録（`~/.claude/projects/<フォルダ名>/<sessionId>.jsonl`）だけが残っている場合、スクリプトはアプリで該当の会話を開いてから送る。この操作で画面がその会話へ切り替わる。画面を変えられないときは `--no-wake` を付ける。`--wake-timeout <秒>` は会話を開くときの待ち時間で、既定は 10 秒。

アプリが終了している、会話を開くための対応情報が残っていないなど、自動で開けない場合は、アプリで該当の会話を開いてから送る。ソケットも記録もない場合は終了コード `3`。

### 受け取れない場合

外部プロセスからの送信には、受信側のセッション間メッセージ設定が適用される。受信側がバイパスモードの場合は、ターミナルで承認待ちとなり、Desktop では承認を求める画面が出ない場合がある。公式文書によると、保留のままなら 5 分で期限切れになる。受信設定の `crossSessionInbound`（`/config` の「Messages from your other sessions」）を確認する。ユーザー設定を変更すると全セッションに適用されるため、変更はユーザーが判断する。[受信設定と制約](https://code.claude.com/docs/en/cross-session-messaging#control-inbound-messages)

## 結果を確認する

`--sender` を付けると送り主の行が増える。メッセージ ID は `--sender` を省略しても付く。同じ宛先と ID の `accepted` が記録されている場合は再送を止める。`unknown` の記録があれば、到達を確かめてから判断する。[到達と再送の共通手順](delivery.md)

ローカルの受信用ソケットは、正常に受け付けても応答の本文が空になる場合がある。結果の JSON には受付を示す `status: accepted` と、会話記録に同じ ID があるかを示す `arrival`（`confirmed` / `unconfirmed`）が入る。会話を開いた場合は `wake: opened` も入る。通信断などで送信結果が不明なら終了コード `1`。

## クラウド会話へ送る

```bash
python3 scripts/claude_send.py send "$cloud_session_id_or_url" "本文"
```

`session_01…` または `https://claude.ai/code/...` を指定すると、`claude -p --cloud` を使う。ローカルの受信ソケットや画面を開く処理は使わず、返事を待たずに戻る。結果は `route: claude-cloud`、`arrival: unconfirmed`。Remote Control 宛てでは保留され、受け手の `crossSessionInbound` が `accept` でないと約 5 分で消えたという報告がある。[Claude Code の報告 #110970](https://github.com/anthropics/claude-code/issues/110970)

## 新しい会話を作る

信頼済みフォルダで `claude --bg -n "$session_name" "最初の指示"` を実行すると、バックグラウンドの新規セッションができる。`claude agents --json` で一覧、`claude logs "$session_id"` で出力、`claude stop "$session_id"` で停止できる。Desktop に新しい会話を作る場合はアプリの新規セッション機能を使う。

## 確認できた範囲

Codex、Cursor、OpenCode、Grok Bot、シェルから届いた例がある。Claude Code 2.1.323 の Desktop では既存会話への受信と Antigravity への返信を確認した。macOS の赤いボタンでウィンドウを閉じても、読み込み済みの会話には届いた。ウィンドウを閉じた状態で未読み込みの会話を開いて送る動作は未確認。
