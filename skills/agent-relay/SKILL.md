---
name: agent-relay
description: 画面（GUI）を通さずに、起動中の別のAIエージェントのセッションへメッセージを送る、新しいセッションを立てる、返事を受け取るときに使う。Claude Code、Codex、OpenCode、Cursor、Grok Bot、Antigravity が対象（デスクトップアプリで開いている会話を含む）。「●●に送っといて」「●●でセッション立てといて」「止まってる方を時間が来たら起こして結果を渡して」といった依頼が対象。
---

# エージェント間の送受信
スクリプトなどの相対パスは、この SKILL.md があるフォルダを基準にします。

複数社の AI をリアルタイムで会談させるためのスキル。ユーザーが席を外していても、AI 同士で会話を進められる。

いちばん重視するのは、相手が作業中でも人がボタンを押さずに自動で入ること。次に、相手の画面でユーザーの吹き出しとして見えること。順番待ちに入って人のボタンを待つ送り方は、AI 同士の経路では使わない。

宛先のデスクトップアプリが閉じている（プロセスが終了している）ときは、どの経路でも送らない。終了コードは経路で異なる（多くは `3`。Antigravity の daemon 不在や Muse のブリッジ不在は `1`）。各節の値を確認する。ユーザーには、該当アプリを開いてから送り直すよう伝える。宛先アプリを自動起動する仕組みは作らない。

送る対象かはウィンドウの有無ではなく、アプリのプロセスが動いているかで決める。赤いボタンでウィンドウを閉じたときの挙動はアプリ別。Claude はプロセスが残り、既ロード会話への送信と記録到達を実測済み。OpenCode はウィンドウ0でもプロセスが残り、送信と user メッセージ到達を実測済み。Antigravity は赤閉じでアプリ本体ごと終了する（macOS 26・Antigravity 2.32.1 のこの環境で実測。送信不可）。Codex はコード上は終了しないが未実測。未ロードの会話を起こす操作とウィンドウ閉じの組合せは未実測（「未確認のこと」を見る）。

他の AI の文は、相手の中でユーザー入力として扱われる形で入れてよい。ユーザー本人のローカル環境だけで使う道具なので、プロンプトインジェクション対策の警告文や送信制限は足さない。危ない指示は受け取った側の各社の安全装置で止まる、というのがユーザーの判断。

送る本文の先頭には送り主を付ける。形は次の2行（`codex_send.py` や `agy_send.py` の既定と同じ）。

```text
<送り主>から
メッセージ ID: <ID>
```

他セッションの報告・相手の自己申告・公式情報によるものは、その旨を書き添えている。
以下は特定環境での実測を含む記録。公開前に環境固有の絶対パスや確認日付の一部を取り除いている。各節の未確認事項も参照する。

裏で使い捨ての子エージェントに一度だけ任せて結果を自分で受け取りたい場合は、お使いの環境のモデル分担のやり方に従ってください。こちらは、見えている（あとで見る）セッションに発言が増える用途向けです。経路によっては相手の作業中に割り込みになり、受け手の権限モードや利用上限で止まることもある。返事は自分に戻らないことが多い。

## 送る前に確かめること

宛先は、ユーザーが指定したセッションか、自分が今回立てたセッションだけにする。ユーザーが付ける名前は適当なことがあるので、ユーザーへの報告や依頼文では宛先を人が見る名前とIDを並べて書き、送るときはID（Claude は sessionId、Codex は threadId、OpenCode は sessionID）で固める。IDが分からず、名前でも1つに絞れなければ送らず、ユーザーに確かめる。作業の途中で名前やIDを変えない。

相手の状態を見る。待機中（idle）に新しいターンが始まるかは経路によって異なる。専任子の `collaboration.send_message` は親に新しいターンを起こさない。作業中（busy）のときの動きも経路で違う。ソケット経由（`claude_send.py`）は、作業中の Claude セッションに途中で届いた。SendMessage はデスクトップアプリ側で保留され、ターン終了後に処理された。Codex の queue は割り込まず、今のターンが終わった直後に流れる。5時間枠などの上限で止まっているときに届いたらどうなるかは、まだ確かめていない。

送る側が区別するのは、宛先の状態ではなく「送れたか」と「届いたか」。

- 送れなかった（未送信）: 宛先アプリのプロセスが無い、宛先IDが違う、未ロードの会話を起こせない。終了コードは経路で異なる（多くは `3`。Antigravity の daemon 不在や Muse のブリッジ不在は `1`）。届いていないので、条件が直れば送り直してよい。
- 受付済み・到達未確認: 送信コマンドは成功したが、会話記録にメッセージ ID がまだ出ていない。
- 届いた: 会話記録に同じメッセージ ID のユーザー行がある。
- 結果不明: 通信が途中で切れた等。終了コード `1`。自動では再送しない。記録を確認し、届いていなければ明示的に再試行してよい。

「会話記録にメッセージ ID があるか」で判定できるのは、その記録を読める経路に限る（Claude・Codex・OpenCode・Antigravity）。Muse と Grok は記録を直接読めないので、各節に書いた確認方法に従う。受付（コマンド成功）・到達（記録や画面で確認）・結果不明は別物として扱い、受付だけで到達とはしない。

未ロード（アプリは動いているが、その会話の受信先が無い）のときの動きは経路で違う。Claude はアプリで会話を開いて受信ソケットを作り、できてから送る。OpenCode は送信そのものがセッションを起こす。Codex は未ロードだと `codex queue` だけでは流れないが、`codex://threads/<threadId>` で読み込ませてから queue に積めば流れる（2026-11-17 実測）。

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

| 送り先 | 作業中に送ったとき | 待機中に送ったとき | 画面での見え方 | アプリのプロセスが無いとき |
| --- | --- | --- | --- | --- |
| Codex（`codex_send.py`） | その場で差し込み | `codex queue` に自動切り替え、すぐ新しい返事が始まる | ユーザーの吹き出し（送り主つき） | 送らずに止まる（終了コード 3） |
| Antigravity アプリ（`agy_send.py send`） | 次の区切りで入る（作業は止まらない） | 新しい返事が始まる | ユーザーの吹き出し | 送らずに止まる（アプリ未起動は 3、daemon 不在は 1） |
| OpenCode（`opencode_send.py`） | 未確認 | 新しい返事が始まる | ユーザーの吹き出し | 送らずに止まる（終了コード 3） |
| Claude Code（`claude_send.py`） | 合間に入る | 新しい返事が始まる | デスクトップアプリでは「別のセッションからのメッセージ」の枠で本文が出る（長いと畳まれる）。ユーザーの吹き出しとは別の見た目 | 送らずに止まる（終了コード 3） |
| Muse（`muse_send.py`） | 未確認 | 届く（数分かかることがある） | ユーザーの吹き出し（ブリッジの見出しつき） | 送らずに止まる（アプリ未起動は 3、ブリッジ不在は 1） |
| Grok Bot（`grokbot_send.py`、Webhook） | Routine が起動して処理する | 同左 | 依頼文は出ず、Bot の結果報告だけがチャットに出る | 送らずに止まる（終了コード 3） |
| Cursor アプリ | 外から入れる方法がない | 同左 | 送る側としては使える | 受け取れない |

### Claude Code

自分が Claude Code なら、SendMessage を使う（ListAgents で相手の名前を見る）。`notify_when_idle: true` を付けると、相手のターンが終わったときに返事の要約が通知で戻る。

それ以外（Codex、Cursor、OpenCode、Grok Bot、シェル）からは、セッションごとの受信ソケットに書く。どれも実際に届いた。Claude Code 2.1.323 のデスクトップアプリで開いている既存会話にも届き、そこから `agy_send.py` で Antigravity へ返信・着弾した。

```bash
python3 scripts/claude_send.py list
python3 scripts/claude_send.py send <名前|PID|sessionId> "本文"
python3 scripts/claude_send.py send <sessionId> --sender Codex --message-id <ID> < 本文.txt
```

`list` は受信ソケットが生きているセッションだけを出す。`send` は一覧に出ない会話も解決する。PID と sessionId（先頭一致可）は明示指定として扱う。名前のときは、`~/.claude/sessions/*.json` の名前と会話記録の表示名（`custom-title`）を全件（585 件・2.2GB で約 0.6 秒）から集めて統合し、1つの会話に定まるまで送らない。同じ sessionId の重複行だけを同じ会話とみなし、受信ソケットが生きている行を使う。sessionId が違う候補（同名の別会話、稼働中と未ロードの同名、記録だけ残った同名など）は、起動中を優先せず候補を並べて送らないので、sessionId を指定し直す。読めなかった記録があるときも、同名の会話が埋もれている可能性があるので送らない。古い sessionId を渡したときは、セッション情報の `hostSessionId` かログの「Mapping internal session local_… to CLI session …」が一致する起動中の行にだけ寄せ、その旨を stderr に出す。アプリ再起動で `local_…` が変わるかは未確認なので、この対応づけは根拠つきの推定として扱う。宛先の決め方は3つ。

- 受信ソケットが生きている: そのまま送る。
- 会話の記録（`~/.claude/projects/<フォルダ名>/<sessionId>.jsonl`）だけがある（未ロード）: アプリでその会話を開いて起こし、受信ソケットができてから送る。起こすと、アプリの表示はその会話に切り替わる。
- どちらも無い: 送らず、終了コード `3`。

起こし方は、アプリがログに残す内部ID（`local_…`）を使い、`claude://code/continue?session=<local_…>` を `open -g` で開く（`-g` なのでアプリを前面に出さない）。アプリが会話を読み込むと `~/.claude/sessions/<pid>.json` ができ、そこに受信ソケットが書かれる。内部IDは `~/Library/Logs/Claude/main*.log` の「Mapping internal session local_… to CLI session …」から引く。ログに無い会話は起こせないので、その旨を出して止まる（アプリで開いてもらう）。アプリのプロセスが無いときは、起こさずに「宛先アプリが閉じています」で止まる。この深いリンクの経路はアプリ内の実装で確認したもので、公式文書にはない。

`--no-wake` を付けると起こさずに止まる。`--wake-timeout <秒>` は起こす待ち時間（既定10秒）。`--sender` を付けると本文の先頭に「`<送り主>`から」「メッセージ ID: `<UUID>`」の2行を付ける。`--message-id` は再送防止用のIDで、同じIDで `accepted` の記録があれば送らない（`--resend` で明示的に再送）。`unknown` は記録を残すだけで、記録を確認したうえでの再試行は妨げない。

結果は JSON で出る。`status` は `accepted`、`arrival` は `confirmed`（同じメッセージ ID が会話記録の末尾にある）か `unconfirmed`、`wake` は起こしたときだけ `opened`。送信結果が不明なときは終了コード `1`。自動では再送せず、記録を確認して届いていなければ明示的に再試行する。

宛先は `~/.claude/sessions/<pid>.json` の `messagingSocketPath`（例 `/tmp/cc-socks/<pid>.sock`）。応答は空なのが正常で、届いた証拠にはならない（「届いたかの確かめ方」を見る）。返事が来ないときの原因調べには、相手の記録 `~/.claude/projects/<フォルダ名>/<sessionId>.jsonl` の末尾を見る。

新しいセッションは `claude --bg -n <名前> "最初の指示"` で立つ。`claude agents --json` で一覧、`claude logs <id>` で出力、`claude stop <id>` で止める。信頼済みのフォルダでしか起動できず、アプリの一覧には出ない。開いているアプリ会話には上記の `claude_send.py` で直接送れる。アプリに新規会話を作るなら、アプリの新規セッション機能を使う。

ソケットは公式文書にある（[セッション間メッセージ](https://code.claude.com/docs/en/cross-session-messaging)、v2.1.224 以降。パスは各セッションの環境変数 `CLAUDE_CODE_MESSAGING_SOCKET` にも入る）。ただし、送る中身の書式は公式文書になく、手元で動いた実績による。

外のプロセス（claude_send.py）から送った文は、受け手がバイパスモードだと保留になる。ターミナルでは承認画面で止まり、5分で捨てられる。デスクトップアプリでは承認画面が出ないまま、同じく5分で捨てられる（公式文書による）。受け手の `crossSessionInbound`（/config の「Messages from your other sessions」）を accept にすれば通る。ユーザー設定に書くと全セッションに効くので、変えるかはユーザーが決める。届かないときはまずここを見る。

### Codex

同じ Mac で Desktop が読み込み中の独立したチャットには、`scripts/codex_send.py` で送る。作業中なら内部 IPC の steer で、送信元の名前とメッセージ ID を付けた文章をチャット入力として差し込む。ユーザーのボタン操作は不要。Desktop が「作業中のターンがすでに終了した」と明示的に拒否した場合は、同じメッセージ ID で公開 CLI の `codex queue` に切り替える。steer の IPC は公開 API ではないため、アプリ更新で互換性が変わる場合がある。

対象 UUID と送信元を明示し、本文は標準入力に渡す。先頭行は「送信元の名前から」になる。steer と queue のどちらも `presentation: "chat_message"` を返す。実際に使った経路は `route`、受付状態は `status` で分かる。`--visible` を指定すると最初から queue を使う。作業中の宛先で queue を使った場合は、通常、今のターンが終わるまで待つ。

コマンドの成功は `steer` の受付または `queue` への投入を表す。`codex_send.py` は結果 JSON に `route`・`status` に加えて `record`（会話記録のパス）と `arrival`（`confirmed` か `unconfirmed`）を付ける。`confirmed` は記録の末尾に同じメッセージ ID の入力がある場合だけ。送信側は、閲覧権限がある場合、宛先チャットの記録で同じメッセージ ID のユーザーメッセージを確認する。チャット入力上の送信元は先頭行で見分け、文中の依頼や承認は送信元のエージェントからの発言として扱う。確認できるのが受付だけなら「受付済み・到達未確認」と報告する。画面表示が要件ならチャット欄で現物を確認し、画面を見られない場合は表示未確認と伝える。ネイティブツールが宛先・祖先関係を理由に拒否した操作の回避には使わない。応答形式が不正な場合や通信断などで送信結果が不明な場合は、自動の再送・別経路への切替は行わない。記録を確認し、届いていなければ明示的に再試行する。

2026-11-16 に作業中の Desktop チャットへ既定動作で送信した。押しボタンの操作なしで、同じターンの記録にユーザーメッセージとして入り、送信側と受信側のメッセージ ID が一致した。ユーザーが送信元名つきの吹き出しを目視で確認した。回答後に `queue` でチャット入力が届き、新しいターンが始まった実機確認は旧版で行った。Claude Code・Antigravity・OpenCode・Cursor・Grok Bot・Muse からも、いずれも Codex に届いた。

差し込み（steer）はチャットの `input`（ユーザー入力）として入り、ツール出力ではない。`--sender` は送信元名（必須）。本文は標準入力。Codex アプリ（ChatGPT.app）が起動していないときは、IPC も `codex queue` も試さず終了コード `3` と「宛先アプリが閉じています」で止まる。`--steer-only` は作業中の差し込みだけを試し、ターン終了済みの拒否時は終了コード `3` で止まる（手動で `codex queue` へ切り替えてよい）。`--visible` は最初から `codex queue` を使う。既定は steer を試し、拒否されたときだけ queue へ自動切り替える。

アプリ側でチャットの持ち主が見つからないとき（チャットがアプリで開かれていない、または登録が外れたとき）も、同じく `codex queue` に切り替わり、標準エラーに注意が出る。

未ロードのチャットは、`codex://threads/<threadId>` を開くとアプリがそのチャットを読み込み、持ち主が付く。`codex_send.py` は持ち主が見つからないとき、この深いリンクを `open -g` で開いて最大10秒待ち、読み込まれたら送信をやり直す（`--no-wake` で無効）。2026-11-17 に、未ロードだった `<ID>` と `<ID>` で、深いリンク後に持ち主が付き、続く queue 送信が記録に載ってターンが始まることを確認した（後者は `codex_send.py` の起床処理を通した実機確認）。深いリンクはアプリ内の実装で確認した経路で、公式文書にはない。起こすとアプリの表示はそのチャットに切り替わる。

相手が作業中なら、通常は今のターンが終わるまで順番待ちになる（作業中の状態）。未ロードのチャットは別で、キューに積まれてもアプリが読み込むまで流れない。未ロードは `codex://threads/<threadId>` で読み込ませてから queue に積む（アプリ内の実装、2026-11-17 実測）。`codex queue --resume` は積んだあとにローカルの CLI デーモンで再開を試みるが、アプリのチャットが同じ画面で開くかは未確認で、`codex_send.py` は使っていない。

フルアクセス（sandbox/approval）は、受け取った後に実行できる操作の範囲で、外部入力の受付とは別。2026-11-17 に、Codex アプリで sandbox `managed`・approval `on-request` の宛先2件（`01a1e034…`、`01a1e35d…`）へ外部から queue を積むと受理され、手動の受信承認なしでターンが始まった。確認できたのはこの範囲だけで、他の sandbox/approval の組み合わせや全条件での受信可否を保証するものではない。steer（作業中への差し込み）は 2026-11-16 に別のチャットで確認済みで、今回の制限付き宛先では試していない。受信後に権限が要る作業をさせる場合だけ、受け手の approval が効く。

登録が外れる原因：`~/.codex/ipc/ipc.sock` は Codex アプリと VS Code の Codex 拡張（`openai.chatgpt`）が共有し、先に起動した側が仲介役になる。VS Code の拡張が仲介役のまま VS Code を閉じると、アプリ内で仲介役の取り合いが起き、外部からの `thread-owner-discovery` がどのチャットでも `no-client-found` になる（2026-11-16 のログで確認、OpenAI 側の未修正の不具合）。拡張はVS Code を開くだけで起動し、参加を止める設定はない。そのため VS Code の Codex 拡張は入れない。入れた場合は、VS Code を閉じたあとに Codex アプリを再起動する。起きてしまったら、Codex アプリの再起動で直る。

```bash
printf '%s' '本文' | python3 scripts/codex_send.py --thread <UUID> --sender <送信元>
```

旧方式の Desktop `start` による作業中の送信後、画面からの送信が active turn ID 不明のエラーになる事象が発生した。既存ターンに重複する仮のターンが残る既知の問題と症状は一致するが、今回の原因は未確定。再起動後に画面送信は回復した。重複した仮ターンを避けるため旧方式は使わない。

待機中や `--visible`・`codex queue` だけを使うときは、取り込まれた文がチャット入力として表示され、新しいターンが始まる。作業中に queue だけ積んだ文は、通常は現在のターンが終わるまで流れない。専任子と受信箱を監視する方式（[旧記録](references/codex-busy-inbox.md)）は、止まっている相手を起こせないことが分かったため、使わない。

```bash
sqlite3 ~/.codex/state_5.sqlite "select id,name,datetime(created_at,'unixepoch','localtime') from threads where archived=0 order by updated_at desc limit 10;"
codex queue --thread <threadId|チャット名> --message "本文"
```

本文には外部 AI からの報告であること、送信元と重複確認用 ID を明記する。queue の成功は投入の証拠であり、親が読んだ証拠ではない。`--resume` の再開失敗は警告だけでコマンド自体は成功するため、警告と親の実際の処理を確認する。中断状態の再開、アプリ終了やスリープ、利用上限の復帰後まで含む確実な自動配送は確認していない。送信結果が不明な場合は同じ内容を自動再送しない。

送った文はアプリのチャットのキュー欄に出る。チャットが待機中ならすぐ流れて、返事が付く。作業中なら通常、今のターンが終わった直後に流れる。`followUpQueueMode = "steer"` は Desktop の入力欄から送る追加入力の設定で、CLI で積んだ文を自動で割り込ませる設定ではない。queue を使い、作業中に早く反映させたい場合は、ユーザーが画面の「今すぐ反映」を押す。直前の作業をユーザーが止めた（中断した）チャットではキューが一時停止していて、ユーザーが「再開」を押すまで流れない。流れたかは `~/.codex/queue_1.sqlite` の `queued_items` から消えたかで分かる。返事は `threads.rollout_path` の記録で、末尾の `task_complete` の `last_agent_message` を読む。

新しいチャットを作る依頼では、Codex の native tool `create_thread` が使えればそれを優先する。`send_message_to_thread` を使う場合はツールが求めるユーザーの明示許可に従う。

CLI の共有デーモン（`~/.codex/app-server-control/app-server-control.sock`）でセッションを作り、続けられる。ただしデスクトップアプリ（ChatGPT.app 内の Codex）は自分専用の app-server を別に動かしていて、このデーモンとは別物。デーモンで作ったセッションはアプリの一覧に出なかった（originator が `codex-tui` になり、アプリ製は `codex_work_desktop`。送る側からは変えられない）。アプリの画面に出したい用途には使えない。

```bash
python3 scripts/codex_daemon.py new "最初の指示" --name <名前> --cwd <フォルダ>
python3 scripts/codex_daemon.py send <threadId> "本文"
```

返事はそのまま表示される。`codex queue` は、アプリかデーモンのどちらかにそのセッションが読み込まれていないと処理されない。`codex exec` で作ったセッションに積んでも、そのままでは動かなかった。`codex queue --resume` は、積んだあとにローカルの CLI デーモンか指定したリモートサーバーで再開を試みる。Desktop で未ロードのチャットが自動で再開するかは未確認。

裏で完結させたいだけなら `codex exec --json ...` で新規、`codex exec --json resume <ID> "本文"` で追記できる。セッションIDは出力の `thread.started` の `thread_id`。ただし exec で作ったセッションは、アプリの一覧に出ない見込み。

Codex を送り手にするとき、実行環境が `workspace-write` かつネットワーク無効（`network_access: false`、デスクトップアプリのワークスペース権限モードや既定設定の CLI 等）の場合は常に、`agy_send.py`・`claude_send.py`・OpenCode API などのローカル通信や外部通信が `operation not permitted` で弾かれる（`agy` 固有ではない）。`on-request`（`auto_review` 等）環境なら `sandbox_permissions: "require_escalated"` 付きの再実行で通る場合があり（2026-11-14 に `agy_send.py` で確認）、CLI 起動なら `--sandbox workspace-write` に `-c sandbox_workspace_write.network_access=true` を付けるとソケット／ネットワーク通信が通る（ただしワークスペース外へのファイル書き込みが必要な経路は別）。

### OpenCode

OpenCode.app のプロセスが動いているときだけ、既存セッションに送れる。画面（ウィンドウ）を閉じていても、プロセスが残っていれば送る対象。プロセスが無いときは `opencode_send.py` が送らず終了コード `3` で止める（常駐 CLI だけが動いていても送らない）。

```bash
opencode api session.list
python3 scripts/opencode_send.py <sessionID> "本文"
python3 scripts/opencode_send.py <sessionID> --sender "<送り主>" --message-id <ID> < /path/to/body.txt
opencode api session.message.list --param sessionID=<ID>
```

画面で開いていないセッションにも届く。送信そのものが新しいターンを起こすので、Claude のような「未ロードを起こす」手順は要らない。2026-11-17 に、8時間前に止まっていたセッションへ送り、API 応答の `data` に `type: "user"` のメッセージ（本文に同じメッセージ ID）が入り、そのあと返事が付くことを実機で確認した。

`--sender` を付けると `codex_send.py` と同じ「送り主から」「メッセージ ID: …」の見出しを付ける。`--message-id` は再送防止用で、同じ宛先・同じIDが `accepted` で残っていれば送らない（`--resend` で明示的に再送）。`unknown` は記録のみで、確認後の再試行を妨げない。成功時は `status`、`arrival`（API 応答にユーザーメッセージが入っていれば `confirmed`）、`message_id`、`message_object_id`、API 応答を JSON で出す。`SessionNotFoundError` は宛先違いなので終了コード `3`。通信の待ち期限切れは結果不明で終了コード `1`。自動再送はせず、記録を確認してから明示的に再試行する。

デスクトップアプリ（OpenCode.app 2.2.7、CLI 2.2.5）で開いている会話にも送れ、そこから `agy_send.py` で Antigravity へ返信・着弾した。アプリが 1.x 系だと保存先が別で `SessionNotFoundError` になる。アプリの自動更新は 1.x の中で止まることがあるので、`brew upgrade --cask opencode-desktop` で CLI と同じ 2.x に揃える。`session.list` は新しい順。

`session.prompt` はすぐ戻り、返事はあとから付く。`session.message.list` の末尾が `{"type":"idle","outcome":"succeeded"}` なら終わっている。`curl` で直接叩くと、一部のパスは認証エラー、ほかは HTML が返る。必ず `opencode api` を使う。返答には `"delivery":"steer"` が付く。作業中のセッションに送ったときの動きは未検証。

新規は `opencode run --title <名前> --format json "最初の指示" < /dev/null`、追記は `opencode run -s <ID> --format json "本文" < /dev/null`。

### Cursor

Cursor アプリの会話の AI は、`codex_send.py` などで他へ送れる。相手が作業中なら、相手の返事は相手の会話記録を読んで受け取れる。相手の作業が終わって止まっているときは、外からは起こせない。

Cursor CLI の会話は裏で動き、デスクトップアプリの画面には吹き出しとして出ない。ユーザーが見ている会談の宛先には使わない。

CLI のチャットには送れる。

```bash
cursor-agent -p --mode plan --trust --model <自社モデル> --output-format json "最初の指示" < /dev/null
cursor-agent -p --mode plan --trust --model <自社モデル> --resume <chatId> "本文" < /dev/null
```

chatId は結果JSONの `session_id`。空のチャットだけ先に作るなら `cursor-agent create-chat`。`composer-2.5-fast` で `--resume` すると返事の本文が空になることがあるので、追記には `composer-2.5` を使う。`cursor-agent ls` は対話画面専用で、スクリプトから呼ぶと固まって残る。

アプリ（Cursor の Agents 画面、This Mac）で開いている会話には、この方法では送れない。アプリの会話IDを `--resume` に渡すと、同じIDの別の CLI チャットが新しくでき、アプリ側の文脈は引き継がれない。公式の Cloud Agents API（`GET /v1/agents`、キーは `~/.config/cursor/api-key`）の一覧（1ページ目の20件で確認）にも、当日の This Mac のチャットは出ず、クラウドと My Machines のエージェントだけが出た。API で立てたエージェントへ `POST /v1/agents/{id}/runs` で追加の指示を送れるか、それがアプリの一覧に出るかは未確認。

### Antigravity

アプリの会話と CLI の会話は別管理で、保存先も送り方も別。既定はアプリ会話で、CLI を扱うときは `--cli` を付ける。Antigravity IDE も `~/.gemini/antigravity` をアプリと共有するが、送信は Antigravity.app 本体の `language_server` だけを使う。IDE 内チャットで開いている会話や IDE が立てる hub には送らない。

```bash
python3 scripts/agy_send.py list
python3 scripts/agy_send.py list --cli
python3 scripts/agy_send.py send <会話ID|タイトル> "本文" [--sender "<送り主名>"]
python3 scripts/agy_send.py send <会話ID|タイトル> "本文" --cli --model <モデル> --cwd <フォルダ>
python3 scripts/agy_send.py new "最初の指示" --title "<名前>"
python3 scripts/agy_send.py new "最初の指示" --cli --model <モデル> --cwd <フォルダ>
```

アプリ会話の `send` は、まず Antigravity.app 本体の `language_server` へ `SendUserCascadeMessage` を投げる。画面ではユーザーの吹き出しとして出る（記録上は `USER_INPUT` / `USER_EXPLICIT`）。相手が待機中なら新しい返事を始め、作業中ならいまの実行へ割り込んで渡す。既定では作業中でも待たずにすぐ送る。`--wait-idle <秒>` を付けたときだけ、作業が終わるまで最大その秒数待ち、それでも作業中なら送信を見合わせて止める。`--sender` を付けると本文の先頭に「`<送り主>`から」「メッセージ ID: `<UUID>`」の2行と空行を付ける（`codex_send.py` と同じ形）。送信結果の表示には経路（`user_input` か `system_message`）とメッセージ ID が出る。

`SendUserCascadeMessage` が RPC 未実装などと明示されたときだけ、従来の `agentapi send-message`（画面では「Message from System」）へ切り替える。通信が途中で切れたなど届いたか分からないときは結果不明として扱い、自動の再送も自動の経路切替もしないで終了する。相手の会話記録（`transcript.jsonl` など）を確認し、届いていなければ同じ会話IDを指定して明示的に再試行する。この場合も、daemon の選択・`Antigravity IDE.app` と CLI の除外・会話IDの一致確認は緩めず、宛先の取り違えを防ぐ。`new` は従来どおり `agentapi new-conversation`、CLI 会話は `agy` の `-p --conversation` を使う。実行ファイルは環境変数 `ANTIGRAVITY_AGENTAPI` / `ANTIGRAVITY_AGY`、`PATH`、標準位置（`~/.gemini/antigravity/bin/agentapi`、`~/.gemini/bin/agy`）の順で探す。

アプリ会話の `send` は、送る前に Antigravity.app が起動しているかを確かめる。アプリと IDE が同時に起動しているときは、daemon の PID が `Antigravity.app` 本体の `language_server` を指すものを優先し、`Antigravity IDE.app` と CLI の `agy` は除外する（2026-11-17 に同時起動で確認）。ただし、Antigravity はウィンドウの赤ボタンで閉じるとアプリ本体と language_server が終了した（2026-11-17 に macOS 26・Antigravity 2.32.1 のこの環境で実測。他版で同じ挙動とは限らない）。Claude や OpenCode のように「窓だけ閉じてプロセス継続」にはならないので、閉じた場合はユーザーにアプリを開いてもらう。無ければ終了コード `3` で止める。`--cli` 経路と `new` はアプリ確認をしない。

接続先の `ANTIGRAVITY_LS_ADDRESS` と `ANTIGRAVITY_CSRF_TOKEN` が両方未設定なら、`~/.gemini/antigravity/daemon/*.json` を新しい順に調べ、生存中の PID の中から `Antigravity.app` 本体のプロセス（`language_server`）を優先して接続先を補う（`Antigravity IDE.app` や CLI が立てる `agy` プロセスへの誤接続を防ぐため、それらは除外する）。Grok Bot の Mac Shell、Muse のブリッジ、他アプリのシェルからも手動設定なしで送れる。片方だけ設定されている場合は誤接続を避けるため停止する。Codex 等のサンドボックスで localhost 接続が `operation not permitted` などで拒否された場合は再実行ヒントが出る。権限が許す環境では `require_escalated` 等で再実行する。

一覧は `~/.gemini/antigravity/conversation_summaries.db` と `~/.gemini/antigravity-cli/conversation_summaries.db` を読み取り専用で開く（サンドボックス等で `-shm` を開けないときは、更新競合を検証しつつ `$TMPDIR` へ一時複製して WAL ごと読み取る）。アプリ側 DB に混ざる CLI 行（`source` が `CORTEX_TRAJECTORY_SOURCE_CLI` 等）は CLI 一覧へ回す。`--cli` を付けずに CLI 会話を指定すると、その旨を出して止まる（`agentapi` は CLI 会話に届かない）。逆も同じ。同名の会話がアプリと CLI の両方にあるときも、勝手に選ばず `[app]` / `[cli]` の候補を出して会話IDの指定を求める。

相手の会話ログで枝分かれ（非ユーザー入力ステップの多重重複や直近の巻き戻り重複）が検出された場合も、暴走を防ぐため送信を止める（分岐時の強制送信は `--force`）。受け手側の Antigravity で他AIからの返信を待つときも、裏コマンドを抱えたままにせず一度ターンを終えて待機する。なお、`send` が返すのは受付だけで、コマンドが終わったことは相手が読んだ証拠にならない。確認は、相手からの `agent-relay` 経由の返信か、人間の目視で行う。

`--cli` 経路（`agy -p`）は違って、相手の応答が返るまでコマンドが待ち、結果を JSON で受け取る。`--title` は `system_message` フォールバック時のみ、`--model` はアプリの `new` または CLI 経路、`--cwd` と `--skip-permissions` は CLI 経路のみ指定できる。`--json` で結果を JSON 形式にできる。既定では権限確認を省略しないため、受け手の権限モードによっては止まる（CLI 操作を無承認で許可したい呼び出し側だけ `--skip-permissions` を明示する）。

2026-11-14 に、Antigravity アプリ会話と Claude Code・Codex・OpenCode・Grok Bot・Muse の各デスクトップ経路で双方向の送受信（`claude_send.py`、`codex queue`、`opencode api`、`grokbot_send.py`、`messages/inbox`、および相手側からの `agy_send.py` / CLI ブリッジ返信）、ならびに Cursor デスクトップから Antigravity アプリ会話への着弾を確認した。

## 条件付き、または送れないもの

### Grok Bot（xAI）

送り手としては、Shell 機能で Mac の machineId を指定すると Mac 上でコマンドを実行できる（指定しないと Grok Bot 側のクラウドで動く）。`scripts/claude_send.py` で Claude Code に届いた。Routine の Mac Shell から `scripts/agy_send.py` を使った Antigravity アプリ会話への返信・着弾も確認した。

受け手としては、開いている会話に外から入れる公式の方法がない。`grokbot://` で入れられるという話も、公式には書かれていない。代わりに Routine の Webhook で起動する。

Grok Bot.app が閉じている、または Mac とつながっていないときは、Webhook は HTTP 200 を返しても Routine の Mac Shell は動かず、送り手には何も返らない。`grokbot_send.py` は送る前にアプリの起動と接続ログを確かめ、駄目なら Webhook を呼ばず終了コード 3 で止める（定型文は `scripts/relay_app_guard.py` と同じ形。Mac 未接続の判定は従来どおり。`--force` で確認を飛ばせる）。

Grok Bot の Mac Shell はスキルフォルダをカレントにしない。Routine に書く claude_send.py や agy_send.py の呼び出しは、相対パス `scripts/` ではなく、手元に入れた agent-relay フォルダへの絶対パスにする。

事前準備（ユーザーの手が要る、最初の1回だけ）。Grok Bot に「Webhook トリガー付きの Routine を agent-inbox という名前で作って。起動されたら本文をそのままチャットに出し、Mac の Shell で `cat "$HOME/Library/Application Support/agent-relay/inbox/grokbot.md"` を読んで、その内容を依頼として作業して。終わったら要約をチャットに投稿し、依頼に返信先があれば Mac の Shell で `python3 <agent-relayスキルフォルダの絶対パス>/scripts/claude_send.py send <返信先> "<要約>"` を実行して返して」と頼む。Routine は、チャット右側の「詳細」タブにある「ルーティン」から開け、詳細パネルに POST先 URL、キー、Authorization ヘッダーが出る。Grok Bot 自身は URL と key を見られない。URL と key はチャットに貼らず、ターミナルでキーチェーンに入れる（コピーしてから実行）。2つ目に入れるのは「キー」欄の値で、「Authorization ヘッダー」欄ではない。

```bash
security add-generic-password -s grokbot-webhook-url -a agent-relay -w "$(pbpaste)"
security add-generic-password -s grokbot-webhook-key -a agent-relay -w "$(pbpaste)"
```

送り方は、依頼本文を `~/Library/Application Support/agent-relay/inbox/grokbot.md` に書き、Webhook を呼び鈴として叩く。Routine の指示文にも同じパスが書かれているので、移すときは両方を揃える。

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

`muse_send.py` は送る前に Muse.app の起動を確認する。閉じていれば終了コード `3` と「宛先アプリが閉じています」で止まるので、ユーザーにアプリを開いてもらい、確認してから送り直す。ブリッジの受付フォルダが無ければ終了コード `1`。

```bash
python3 scripts/muse_send.py "本文" --sender "<送り主>" --subject "<件名>" --reply "<返信方法>"
printf '%s' '本文' | python3 scripts/muse_send.py --sender "<送り主>" --subject "<件名>" --reply "<返信方法>"
```

受付フォルダは環境変数 `MUSE_BRIDGE_INBOX`（未設定時は `~/dev/tools/muse-tasha-bridge/messages/inbox`）。フォルダが無ければ「ブリッジが見つかりません」で終了コード `1`。置いたファイル名を標準出力に出す。中身は送り主・件名・本文・返信方法（返信先:）の Markdown。一時名で書いてから `.md` にリネームする。

Muse（Meta のデスクトップアプリ Muse.app）へは、このスキルとは別の Mac 用 `muse-tasha-bridge` を使う。ブリッジを手元に用意したうえで、上記スクリプトが `messages/inbox/` へ依頼 Markdown を置く（ブリッジの取得先と置き場所はブリッジ側の README に従う）。ブリッジが他の AI からの依頼と出典を明示し、本文を Muse のメイン会話へ送る。Muse の応答と必要な作業の完了を別に確認する。

2026-11-14 に、Antigravity から `messages/inbox/` 経由で Muse への配送と、Muse から `cli/inbox` に `args` オブジェクト形式の JSON（`kind: "agy_send"`）を渡し `agy_send.py` 経由で Antigravity に届くことを確認した。形式と設置方法はブリッジの README を参照する。

Codex へ送るときは、ブリッジの `codex_send` ジョブを使う。作業中の Codex にも steer ですぐ届く。`codex queue` だけだと、作業中はそのターンが終わるまで待たされる。

この経路は Muse Gadget の登録、非公式 muse-client、常駐ブリッジが必要で、公開スキルには同梱していない。送信結果が不明なときは自動再送しない。詳細はブリッジの README を参照する。最初のファイル参照による依頼は Muse が拒否した。その後ユーザーが委任を伝え、本文を渡す方式で Codex → Muse → Codex の往復を追加の人手なしに確認した。受信側の権限・安全判断は別途適用され、委任設定が将来も維持されるかは未確認。

Muse Code（Meta のコーディングエージェント CLI、Muse.app とは別製品）は、Muse Code の対話セッション同士なら公式のセッション間メッセージがある。受け手の承認が要り、30分で期限切れになる。外の別プロセスから送れるかは未確認。

ChatGPT の dots は、外から会話に文章を入れる公式の方法がない。自前の MCP サーバーから MCP Events を送れば dot を動かせるが、会話にメッセージとして出るかは未確認。

## 時間指定で起こす

使用量制限が復活する時刻にリマインダー的なものを送るなら、送る処理を時刻指定で動かす。Claude Code 自身を起こすだけなら、そのセッションの CronCreate（1回限り、`recurring: false`）が一番軽い。ただしセッション内だけの登録で、アプリを終了すると消え、Mac がスリープ中は動かない（未実測）。それ以外は、Claude デスクトップの予約タスク、または launchd の1回限りのジョブ（送り終えたら自分で `launchctl bootout` する）を使う。予約タスクは保存したときの権限モードのまま動くとされる（未実測）。

## 届いたかの確かめ方

送信コマンドが成功しても、相手が読んだ証拠にはならない。届いたと言う前に、送ったときのメッセージ ID が相手の会話記録に載っているか確かめる。載っていなければ「受付済み・到達未確認」と伝える。

Codex は `~/.codex/sessions` 配下の当該チャットの記録で、同じメッセージ ID のユーザーメッセージを探す。Claude は `~/.claude/projects/<フォルダ名>/<sessionId>.jsonl` で、`"type":"user"` の行に同じ ID が入っているか見る（2026-11-17 に、この形で「Another Claude session sent a message: …」のユーザー行を確認した）。OpenCode は `session.prompt` の応答 `data` が `type: "user"` で、本文に同じ ID が入っていれば届いている。Antigravity アプリは `~/.gemini/antigravity/brain/<会話ID>/.system_generated/logs/transcript.jsonl` で同じ ID または先頭行の送り主行を探す。

`claude_send.py` の `arrival` は送信直後に記録の末尾を見る。`unconfirmed` でも、少し遅れて載ることがある。返事が来ないことは未着の証拠にならない。

再送の判断は記録の状態で分ける。到達済み（会話記録に同じメッセージ ID がある）なら再送しない。受付済み（`accepted`）と結果不明（`unknown`）は、まず会話記録を確認し、届いていなければ明示的に再試行する。再試行の方法は経路で違う。`--resend` を持つ `claude_send.py`・`codex_send.py`・`opencode_send.py` はそれを付け、持たない Antigravity・Muse・Grok は新しいメッセージ ID で送り直す。記録の確認と `--resend` は、その手段がある経路だけの話で、Muse と Grok は各節の確認方法に従う。再試行してよいのは未送信のときだけではない。受付済み・結果不明も、届いていないと確認できれば明示的に再試行する。ただし受付と到達と結果不明は区別し、受付だけで到達とみなさない。未送信（終了コード `3`）は、宛先アプリを開くなど条件が直れば通常どおり送り直してよい。同じメッセージ ID で送り直すと、`accepted` の記録がある場合だけ止める。再送防止の記録は一時領域（`$TMPDIR/agent-relay-<uid>/state`）に置く（環境変数 `AGENT_RELAY_STATE_DIR` で変えられる）。直近の再送を止めるだけの短命なデータで、再起動や掃除で消えることがある。Codex 宛てで配送結果が不明な場合や、ファイルの変更などを伴う依頼でも、二重処理を避けるため自動再送しない。

「届いた」と言えるのは、相手の会話記録に同じメッセージ ID（Claude は送り主行でもよい）が載ったとき。記録に載る前は「受付済み・到達未確認」と伝える。返事は「相手が読んで処理を始めた」証拠で、記録到達とは別に扱う。返事がなくても記録に載っていれば到達済みと報告してよい。DONE や返事に、お礼や確認の返事をしない。AI同士だと往復が止まらなくなる。

## 未確認のこと

- Codex アプリのチャットが利用上限で止まっている間に積んだ文が、上限が戻ったあと自動で流れるか。
- OpenCode の CLI で作ったセッションが、2.x のデスクトップアプリの一覧に出るか（保存先は同じになったが、画面では見ていない）。
- 利用上限で止まっている相手に届いたときの動き。
- ソケットに書く中身の書式は公式文書になく、Claude Code 2.1.323 のデスクトップアプリ会話への到達は確認済み。今後の版上げで届かなくなる可能性がある。
- 赤ボタンでウィンドウを閉じた状態の実測: Claude はプロセス継続・既ロード会話への送信と記録到達を確認。OpenCode はウィンドウ0・プロセス稼働で送信と user メッセージ到達を確認。Antigravity は赤閉じでアプリ本体ごと終了する（macOS 26・Antigravity 2.32.1 のこの環境で実測。送信不可）。Codex はコード上は終了しないが未実測。未ロードの会話を起こす操作とウィンドウ閉じの組合せは未実測。
- Claude の起こし方は、アプリがログに残す内部ID（`local_…`）に依存する。ログがローテートで消えた会話は起こせない。
- `claude://code/continue?session=local_…` はアプリ内の実装で確認した経路で、公式文書にはない。アプリ更新で使えなくなる可能性がある。起こすと、アプリの表示はその会話に切り替わる。
- ウィンドウを閉じた状態（プロセスは動いている）で、Codex の未ロード会話を `codex://threads/<id>` から起こして queue が流れるかは未実測。`codex queue --resume` がアプリの画面で同じチャットを開くかも未確認。
