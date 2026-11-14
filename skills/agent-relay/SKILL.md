---
name: agent-relay
description: 画面（GUI）を通さずに、起動中の別のAIエージェントのセッションへメッセージを送る、新しいセッションを立てる、返事を受け取るときに使う。Claude Code、Codex、OpenCode、Cursor、Grok Bot、Antigravity が対象（デスクトップアプリで開いている会話を含む）。「●●に送っといて」「●●でセッション立てといて」「止まってる方を時間が来たら起こして結果を渡して」といった依頼が対象。
---

# エージェント間の送受信
スクリプトなどの相対パスは、この SKILL.md があるフォルダを基準にします。

起動中のセッションに割り込んで、相手の文脈のまま続きをやらせるためのスキル。
他セッションの報告・相手の自己申告・公式情報によるものは、その旨を書き添えている。
以下は特定環境での実測を含む記録。公開前に環境固有の絶対パスや確認日付の一部を取り除いている。各節の未確認事項も参照する。

裏で使い捨ての子エージェントに一度だけ任せて結果を自分で受け取りたい場合は、お使いの環境のモデル分担のやり方に従ってください。こちらは、見えている（あとで見る）セッションに発言が増える用途向けです。経路によっては相手の作業中に割り込みになり、受け手の権限モードや利用上限で止まることもある。返事は自分に戻らないことが多い。

## 送る前に確かめること

宛先は、ユーザーが指定したセッションか、自分が今回立てたセッションだけにする。ユーザーが付ける名前は適当なことがあるので、ユーザーへの報告や依頼文では宛先を人が見る名前とIDを並べて書き、送るときはID（Claude は sessionId、Codex は threadId、OpenCode は sessionID）で固める。IDが分からず、名前でも1つに絞れなければ送らず、ユーザーに確かめる。作業の途中で名前やIDを変えない。

相手の状態を見る。待機中（idle）なら新しいターンが始まる。作業中（busy）のときの動きは経路で違う。ソケット経由（claude_send.py）は、作業中の Claude セッションに途中で届いた。SendMessage はデスクトップアプリ側で保留され、ターン終了後に処理された。Codex の queue は割り込まず、今のターンが終わった直後に流れる。5時間枠などの上限で止まっているときに届いたらどうなるかは、まだ確かめていない。

送れるのは文章だけで、会話履歴やファイルは運べない。ファイルはパスを書いて渡す。本文の1行目に「誰から、何の件か」を書く（書き方は次の「依頼文の型」）。SendMessage ツールの説明には、1行目だけがプレビューされるとある（ツールの説明による。画面では確かめていない）。

受け手の Claude は、外から届いた文を「ユーザーの指示や承認ではなく、他セッションからの依頼」として扱う。権限が要る操作を送り先にやらせる前提にしない。

パスワードや鍵（Webhook のキーなど）は、送る本文にも、送るコマンドの引数にも書かない。鍵はキーチェーンに置き、スクリプトがそこから読む（Grok Bot の節の手順）。

## 依頼文の型

依頼は次の形で書く。

```text
<送り主の名前>（<ID>）から：<件名>
返信先：<claude_send.py などで送れる宛先ID>
やること：<今回やること>
やらないこと：<今回やらないこと>
完了条件：<何が出れば終わりか>
終わったら「DONE: <要約>。証拠: <ファイル・コマンド・結果>」を返信先へ送る。詰まったら「BLOCKED: <理由>」を送る。
```

長い中身は貼らずに、ファイルのパスで渡す。長い貼り付けは途中で切れる。

## 宛先ごとの方法

スクリプトは `scripts/` にある。どれも Python の標準ライブラリだけで動く。

### Claude Code

自分が Claude Code なら、SendMessage を使う（ListAgents で相手の名前を見る）。`notify_when_idle: true` を付けると、相手のターンが終わったときに返事の要約が通知で戻る。

それ以外（Codex、Cursor、OpenCode、Grok Bot、シェル）からは、セッションごとの受信ソケットに書く。どれも実際に届いた。Claude Code 2.1.323 のデスクトップアプリで開いている既存会話にも届き、そこから `agy_send.py` で Antigravity へ返信・着弾した。

```bash
python3 scripts/claude_send.py list
python3 scripts/claude_send.py send <名前|PID|sessionId> "本文"
```

宛先は `~/.claude/sessions/<pid>.json` の `messagingSocketPath`（例 `/tmp/cc-socks/<pid>.sock`）。応答は空なのが正常で、届いた証拠にはならない（「届いたかの確かめ方」を見る）。返事が来ないときの原因調べには、相手の記録 `~/.claude/projects/<フォルダ名>/<sessionId>.jsonl` の末尾を見る。

新しいセッションは `claude --bg -n <名前> "最初の指示"` で立つ。`claude agents --json` で一覧、`claude logs <id>` で出力、`claude stop <id>` で止める。信頼済みのフォルダでしか起動できず、アプリの一覧には出ない。開いているアプリ会話には上記の `claude_send.py` で直接送れる。アプリに新規会話を作るなら、アプリの新規セッション機能を使う。

ソケットは公式文書にある（[セッション間メッセージ](https://code.claude.com/docs/en/cross-session-messaging)、v2.1.224 以降。パスは各セッションの環境変数 `CLAUDE_CODE_MESSAGING_SOCKET` にも入る）。ただし、送る中身の書式は公式文書になく、手元で動いた実績による。

外のプロセス（claude_send.py）から送った文は、受け手がバイパスモードだと保留になる。ターミナルでは承認画面で止まり、5分で捨てられる。デスクトップアプリでは承認画面が出ないまま、同じく5分で捨てられる（公式文書による）。受け手の `crossSessionInbound`（/config の「Messages from your other sessions」）を accept にすれば通る。ユーザー設定に書くと全セッションに効くので、変えるかはユーザーが決める。届かないときはまずここを見る。

### Codex

アプリ（ChatGPT.app 内の Codex）で開いているチャットへは、`codex queue` で送る。codex-cli 0.167.2 で到達を確認した。

```bash
sqlite3 ~/.codex/state_5.sqlite "select id,name,datetime(created_at,'unixepoch','localtime') from threads where archived=0 order by updated_at desc limit 10;"
codex queue --thread <threadId|チャット名> --message "本文"
```

送った文はアプリのチャットのキュー欄に出る。チャットが待機中ならすぐ流れて、返事が付く。作業中でも割り込まず、今のターンが終わった直後に自動で流れる（設定 `followUpQueueMode = "steer"` があっても、外から積んだ文は割り込まなかった）。割り込ませたいときは、ユーザーに画面の「今すぐ反映」を押してもらうしかない。直前の作業をユーザーが止めた（中断した）チャットではキューが一時停止していて、ユーザーが「再開」を押すまで流れない。流れたかは `~/.codex/queue_1.sqlite` の `queued_items` から消えたかで分かる。返事は `threads.rollout_path` の記録で、末尾の `task_complete` の `last_agent_message` を読む。

アプリの画面に出る新しいチャットを外から作る方法は、まだ見つかっていない。

CLI の共有デーモン（`~/.codex/app-server-control/app-server-control.sock`）でセッションを作り、続けられる。ただしデスクトップアプリ（ChatGPT.app 内の Codex）は自分専用の app-server を別に動かしていて、このデーモンとは別物。デーモンで作ったセッションはアプリの一覧に出なかった（originator が `codex-tui` になり、アプリ製は `codex_work_desktop`。送る側からは変えられない）。アプリの画面に出したい用途には使えない。

```bash
python3 scripts/codex_daemon.py new "最初の指示" --name <名前> --cwd <フォルダ>
python3 scripts/codex_daemon.py send <threadId> "本文"
```

返事はそのまま表示される。`codex queue` は、アプリかデーモンのどちらかにそのセッションが読み込まれていないと処理されない。`codex exec` で作ったセッションに積んでも、そのままでは動かなかった。`codex queue --resume` が 0.166.0 で公式に入った。`--resume` を付けると、読み込まれていないスレッドでも、積んだあとに再開する。exec で作ったセッションに効くかは未確認。

裏で完結させたいだけなら `codex exec --json ...` で新規、`codex exec --json resume <ID> "本文"` で追記できる。セッションIDは出力の `thread.started` の `thread_id`。ただし exec で作ったセッションは、アプリの一覧に出ない見込み。

Codex を送り手にするとき、実行環境が `workspace-write` かつネットワーク無効（`network_access: false`、デスクトップアプリのワークスペース権限モードや既定設定の CLI 等）の場合は常に、`agy_send.py`・`claude_send.py`・OpenCode API などのローカル通信や外部通信が `operation not permitted` で弾かれる（`agy` 固有ではない）。`on-request`（`auto_review` 等）環境なら `sandbox_permissions: "require_escalated"` 付きの再実行で通る場合があり（2026-11-14 に `agy_send.py` で確認）、CLI 起動なら `--sandbox workspace-write` に `-c sandbox_workspace_write.network_access=true` を付けるとソケット／ネットワーク通信が通る（ただしワークスペース外へのファイル書き込みが必要な経路は別）。

### OpenCode

常駐サービス経由で、既存のセッションに送れる。

```bash
opencode api session.list
opencode api session.prompt --param sessionID=<ID> -d '{"text":"本文"}'
opencode api session.message.list --param sessionID=<ID>
```

デスクトップアプリ（OpenCode.app 2.2.7、CLI 2.2.5）で開いている会話にも送れ、そこから `agy_send.py` で Antigravity へ返信・着弾した。アプリが 1.x 系だと保存先が別で `SessionNotFoundError` になる。アプリの自動更新は 1.x の中で止まることがあるので、`brew upgrade --cask opencode-desktop` で CLI と同じ 2.x に揃える。`session.list` は新しい順。

`session.prompt` はすぐ戻り、返事はあとから付く。`session.message.list` の末尾が `{"type":"idle","outcome":"succeeded"}` なら終わっている。`curl` で直接叩くと、一部のパスは認証エラー、ほかは HTML が返る。必ず `opencode api` を使う。返答には `"delivery":"steer"` が付く。作業中のセッションに送ったときの動きは未検証。

新規は `opencode run --title <名前> --format json "最初の指示" < /dev/null`、追記は `opencode run -s <ID> --format json "本文" < /dev/null`。

### Cursor

CLI のチャットには送れる。

```bash
cursor-agent -p --mode plan --trust --model <自社モデル> --output-format json "最初の指示" < /dev/null
cursor-agent -p --mode plan --trust --model <自社モデル> --resume <chatId> "本文" < /dev/null
```

chatId は結果JSONの `session_id`。空のチャットだけ先に作るなら `cursor-agent create-chat`。`composer-2.5-fast` で `--resume` すると返事の本文が空になることがあるので、追記には `composer-2.5` を使う。`cursor-agent ls` は対話画面専用で、スクリプトから呼ぶと固まって残る。

アプリ（Cursor の Agents 画面、This Mac）で開いている会話には、この方法では送れない。アプリの会話IDを `--resume` に渡すと、同じIDの別の CLI チャットが新しくでき、アプリ側の文脈は引き継がれない。公式の Cloud Agents API（`GET /v1/agents`、キーは `~/.config/cursor/api-key`）の一覧（1ページ目の20件で確認）にも、当日の This Mac のチャットは出ず、クラウドと My Machines のエージェントだけが出た。API で立てたエージェントへ `POST /v1/agents/{id}/runs` で追加の指示を送れるか、それがアプリの一覧に出るかは未確認。

### Antigravity

アプリの会話と CLI の会話は別管理で、保存先も送り方も別。既定はアプリ会話で、CLI を扱うときは `--cli` を付ける。

```bash
python3 scripts/agy_send.py list
python3 scripts/agy_send.py list --cli
python3 scripts/agy_send.py send <会話ID|タイトル> "本文" --title "<通知タイトル>"
python3 scripts/agy_send.py send <会話ID|タイトル> "本文" --cli --model <モデル> --cwd <フォルダ>
python3 scripts/agy_send.py new "最初の指示" --title "<名前>"
python3 scripts/agy_send.py new "最初の指示" --cli --model <モデル> --cwd <フォルダ>
```

アプリ会話は `agentapi` の `send-message` / `new-conversation`、CLI 会話は `agy` の `-p --conversation` を使う。実行ファイルは環境変数 `ANTIGRAVITY_AGENTAPI` / `ANTIGRAVITY_AGY`、`PATH`、標準位置（`~/.gemini/antigravity/bin/agentapi`、`~/.gemini/bin/agy`）の順で探す。

接続先の `ANTIGRAVITY_LS_ADDRESS` と `ANTIGRAVITY_CSRF_TOKEN` が両方未設定なら、`~/.gemini/antigravity/daemon/*.json` を新しい順に調べ、生存中の PID の接続先を補う。Grok Bot の Mac Shell、Muse のブリッジ、他アプリのシェルからも手動設定なしで送れる。片方だけ設定されている場合は誤接続を避けるため停止する。Codex 等のサンドボックスで localhost 接続が `operation not permitted` などで拒否された場合は再実行ヒントが出る。権限が許す環境では `require_escalated` 等で再実行する。

一覧は `~/.gemini/antigravity/conversation_summaries.db` と `~/.gemini/antigravity-cli/conversation_summaries.db` を読み取り専用で開く（サンドボックス等で `-shm` を開けないときは、更新競合を検証しつつ `$TMPDIR` へ一時複製して WAL ごと読み取る）。アプリ側 DB に混ざる CLI 行（`source` が `CORTEX_TRAJECTORY_SOURCE_CLI` 等）は CLI 一覧へ回す。`--cli` を付けずに CLI 会話を指定すると、その旨を出して止まる（`agentapi` は CLI 会話に届かない）。逆も同じ。同名の会話がアプリと CLI の両方にあるときも、勝手に選ばず `[app]` / `[cli]` の候補を出して会話IDの指定を求める。

アプリ経路（`agentapi`）の `send` が返すのは受付だけで、コマンドが終わったことは相手が読んだ証拠にならない。相手が処理中のときに送った文は、順番にキュー処理される（実測）。確認は、相手からの `agent-relay` 経由の返信か、人間の目視で行う。

`--cli` 経路（`agy -p`）は違って、相手の応答が返るまでコマンドが待ち、結果を JSON で受け取る。`--title` はアプリ経路のみ、`--model` はアプリの `new` または CLI 経路、`--cwd` と `--skip-permissions` は CLI 経路のみ指定できる。`--json` で結果を JSON 形式にできる。既定では権限確認を省略しないため、受け手の権限モードによっては止まる（CLI 操作を無承認で許可したい呼び出し側だけ `--skip-permissions` を明示する）。

2026-11-14 に、Antigravity アプリ会話と Claude Code・Codex・OpenCode・Grok Bot・Muse の各デスクトップ経路で双方向の送受信（`claude_send.py`、`codex queue`、`opencode api`、`grokbot_send.py`、`messages/inbox`、および相手側からの `agy_send.py` / CLI ブリッジ返信）、ならびに Cursor デスクトップから Antigravity アプリ会話への着弾を確認した。

## 条件付き、または送れないもの

### Grok Bot（xAI）

送り手としては、Shell 機能で Mac の machineId を指定すると Mac 上でコマンドを実行できる（指定しないと Grok Bot 側のクラウドで動く）。`scripts/claude_send.py` で Claude Code に届いた。Routine の Mac Shell から `scripts/agy_send.py` を使った Antigravity アプリ会話への返信・着弾も確認した。

受け手としては、開いている会話に外から入れる公式の方法がない。`grokbot://` で入れられるという話も、公式には書かれていない。代わりに Routine の Webhook で起動する。

Grok Bot の Mac Shell はスキルフォルダをカレントにしない。Routine に書く claude_send.py や agy_send.py の呼び出しは、相対パス `scripts/` ではなく、手元に入れた agent-relay フォルダへの絶対パスにする。

事前準備（ユーザーの手が要る、最初の1回だけ）。Grok Bot に「Webhook トリガー付きの Routine を agent-inbox という名前で作って。起動されたら本文をそのままチャットに出し、Mac の Shell で `cat ~/.agents/agent-inbox/grokbot.md` を読んで、その内容を依頼として作業して。終わったら要約をチャットに投稿し、依頼に返信先があれば Mac の Shell で `python3 <agent-relayスキルフォルダの絶対パス>/scripts/claude_send.py send <返信先> "<要約>"` を実行して返して」と頼む。Routine は、チャット右側の「詳細」タブにある「ルーティン」から開け、詳細パネルに POST先 URL、キー、Authorization ヘッダーが出る。Grok Bot 自身は URL と key を見られない。URL と key はチャットに貼らず、ターミナルでキーチェーンに入れる（コピーしてから実行）。2つ目に入れるのは「キー」欄の値で、「Authorization ヘッダー」欄ではない。

```bash
security add-generic-password -s grokbot-webhook-url -a agent-relay -w "$(pbpaste)"
security add-generic-password -s grokbot-webhook-key -a agent-relay -w "$(pbpaste)"
```

送り方は、依頼本文を `~/.agents/agent-inbox/grokbot.md` に書き、Webhook を呼び鈴として叩く。

```bash
python3 scripts/grokbot_send.py send "依頼本文" --from "自分のアプリ名"
```

`--from` を省略した場合、送信元は `unknown` になる。依頼は同じファイルを上書きするため、前の依頼を Grok Bot が読む前に続けて送ると、本文が失われることがある。

宛先が分かるときは、grokbot.md に宛先の Bot 名かIDを書く。分からないときは書かなくてよい。agent-inbox 用の受付 Routine は grokbot.md の本文を読み、宛先の記載があればその Bot に渡し、なければ Routine を組んだ Bot が依頼として処理する（振り分けの細部は Routine の指示文で決める）。返事がほしいときは、依頼に「返信先: <claude_send.py の宛先>」を書く。

確認できたこと:
- `Authorization: Bearer <key>` を付けた POST で、応答は HTTP 200、中身は `{"success":true,"runUuid":"..."}`。
- Grok Bot のチャットに、Webhook の本文（JSON）がそのまま表示された。本文は届く。
- Webhook の結果は、Routine を作ったチャットに続けて出る（画面で確認）。
- Routine の POST先 URL は `api2.cursor.sh/automations/webhook/...`（画面で確認）。
- Mac の Shell で `cat grokbot.md` が実行され、依頼として読まれ、結果がチャットに投稿された。`echo ok` も `cat` も承認カードは出なかった。
- `grokbot_send.py` 経由でも 200 が返った。
- 起動結果は Bot のチャットに出るだけで、送り手には返らない。
- 一往復が通った。Claude から `grokbot_send.py send` で「返信先: <claude_send.py の宛先>」付きの依頼を送ると、Routine の中で Grok Bot が Mac の Shell から `claude_send.py` を実行し、送り元の Claude セッションに返事が届いた。python3 の実行も承認待ちにならなかった（ユーザーは同じ時間帯に Grok Bot の画面を見ていた可能性がある）。

未確認のこと:
- Routine が毎回会話の文脈を使うか。前回のやり取りを踏まえた返事もあったが、毎回かどうかは確かめていない。
- 「常に許可」への変更は全 Bot に効くので、ユーザーの判断に任せる。
- 背景作業の承認カードの30分期限と、1時間300回のレート上限。公式情報で、実測していない。

key は漏らさない。ログやチャットに出さない。再発行したらキーチェーンを `-U` 付きで上書きする。

### Muse、Muse Code、dots

Muse（Meta のデスクトップアプリ Muse.app）へは、このスキルとは別の Mac 用 `muse-tasha-bridge` を使う。ブリッジを手元に用意したうえで、その `messages/inbox/` に送り主・件名・本文・返信方法を含む、名前が重複しない Markdown ファイルを置く（ブリッジの取得先と置き場所はブリッジ側の README に従う）。書きかけを読まれないよう、一時名で書いてから `.md` へ変更する。ブリッジが他の AI からの依頼と出典を明示し、本文を Muse のメイン会話へ送る。Muse の応答と必要な作業の完了を別に確認する。

2026-11-14 に、Antigravity から `messages/inbox/` 経由で Muse への配送と、Muse から `cli/inbox` に `args` オブジェクト形式の JSON（`kind: "agy_send"`）を渡し `agy_send.py` 経由で Antigravity に届くことを確認した。形式と設置方法はブリッジの README を参照する。

この経路は Muse Gadget の登録、非公式 muse-client、常駐ブリッジが必要で、公開スキルには同梱していない。送信結果が不明なときは自動再送しない。詳細はブリッジの README を参照する。最初のファイル参照による依頼は Muse が拒否した。その後ユーザーが委任を伝え、本文を渡す方式で Codex → Muse → Codex の往復を追加の人手なしに確認した。受信側の権限・安全判断は別途適用され、委任設定が将来も維持されるかは未確認。

Muse Code（Meta のコーディングエージェント CLI、Muse.app とは別製品）は、Muse Code の対話セッション同士なら公式のセッション間メッセージがある。受け手の承認が要り、30分で期限切れになる。外の別プロセスから送れるかは未確認。

ChatGPT の dots は、外から会話に文章を入れる公式の方法がない。自前の MCP サーバーから MCP Events を送れば dot を動かせるが、会話にメッセージとして出るかは未確認。

## 時間指定で起こす

使用量制限が復活する時刻にリマインダー的なものを送るなら、送る処理を時刻指定で動かす。Claude Code 自身を起こすだけなら、そのセッションの CronCreate（1回限り、`recurring: false`）が一番軽い。ただしセッション内だけの登録で、アプリを終了すると消え、Mac がスリープ中は動かない（未実測）。それ以外は、Claude デスクトップの予約タスク、または launchd の1回限りのジョブ（送り終えたら自分で `launchctl bootout` する）を使う。予約タスクは保存したときの権限モードのまま動くとされる（未実測）。

## 届いたかの確かめ方

送信コマンドが成功しても、相手が読んだ証拠にはならない。証拠は相手の返事だけ。返事が来るまでは、ユーザーに「届いた」と言わず、未確認と伝える。

送るのは1回。起動したばかりの相手は、最初の返事まで30〜60秒かかる。返事が来なければ、相手が一覧にいるか確かめて1回だけ再送し、それでも来なければユーザーに報告する。ただし、ファイルの変更などを伴う依頼は、相手がまだ作業中かもしれず、再送すると二重に作業させるので、再送せずにユーザーに報告する。

DONE や返事に、お礼や確認の返事をしない。AI同士だと往復が止まらなくなる。

## 未確認のこと

- Codex アプリのチャットが利用上限で止まっている間に積んだ文が、上限が戻ったあと自動で流れるか。
- OpenCode の CLI で作ったセッションが、2.x のデスクトップアプリの一覧に出るか（保存先は同じになったが、画面では見ていない）。
- 利用上限で止まっている相手に届いたときの動き。
- ソケットに書く中身の書式は公式文書になく、Claude Code 2.1.323 のデスクトップアプリ会話への到達は確認済み。今後の版上げで届かなくなる可能性がある。
