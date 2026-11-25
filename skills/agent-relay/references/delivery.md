# 到達確認・再送・時刻指定

## 宛先のどこを確認するか

宛先の会話本文に、送った文が同じメッセージ ID で表示または記録されていれば「到達」とする。キューに同じ ID があっても、本文に入るまでは「受付済み」。ID を付けない経路では、送り主と本文を照合する。

| 宛先 | 確認する場所 |
| --- | --- |
| Codex の公式 App Server | 対象ターンを読み取れる公式の手段で、入力のメッセージ ID とターン ID を照合する。 |
| Codex のローカルキュー・Desktop の既存連携 | `record` が示す記録か、`~/.codex/sessions` 内の該当する記録を見る。キューから消えた後も、会話に載ったことを確かめる。 |
| Claude Code | `~/.claude/projects/<フォルダ名>/<sessionId>.jsonl` の `"type":"user"` の行を見る。 |
| OpenCode | `session.prompt` の応答 `data` か、該当セッションの `session.message.list` を見る。 |
| Antigravity | 該当会話の `transcript.jsonl` を閲覧できる場合は、その記録で照合する。 |
| Grok Bot・Muse | [各経路の手順](other-apps.md)に従い、宛先の画面か指定した返信先を見る。 |

`claude_send.py` の `arrival` は送った直後に得られた結果。後から記録に載る場合もある。Codex の返事を記録から読むときは、該当ターンの `task_complete` にある `last_agent_message` を確認する。入力に沿った返信や処理で読了を確かめ、依頼した作業の完了は成果物や実行結果で判断する。

## もう一度送る前に

1. 未送信が確定した場合は、アプリを起動するなど、送れなかった理由を解消して再試行する。多くの経路では終了コード `3`。Antigravity の接続先や Muse のブリッジが見つからない場合は `1` なので、コードと理由を合わせて読む。重複防止で止まった場合も `3` が返る。前の送信結果は記録で確認する。
2. 受付済みの場合や、途中で通信が切れて結果が分からない場合は、キュー、宛先の会話記録、返信を確認する。到達済みや順番待ちなら再送を省く。結果不明のまま別経路に切り替えると、同じ内容が二重に届くおそれがある。
3. キューにも会話にも届かず、最初の送信が後から届く可能性もないと確認できた場合は、同じ宛先にもう一度送る。Claude Code、Codex、OpenCode で同じ ID を明示的に再送するときは、各送信スクリプトの `--resend` を使う。他の経路では、再試行として新しく送ったことが分かるように記録する。

Codex の再送防止は、同じ宛先とメッセージ ID の `accepted` または `unknown` の記録を使う。Claude Code と OpenCode で防止に使うのは `accepted`。記録は一時領域（既定は `$TMPDIR/agent-relay-<uid>/state`、指定は `AGENT_RELAY_STATE_DIR`）にあり、再起動や掃除で消える場合がある。ID が同じでも自動再送はしない。

## 送信元の権限

Codex の `workspace-write` でネットワークが無効だと、ローカルソケットやネットワークへの接続が `operation not permitted` になる場合がある。許可付き再実行が用意された環境では、その承認手順に従う。CLI を起動する環境でネットワーク利用が許可されている場合は、`--sandbox workspace-write` と `-c sandbox_workspace_write.network_access=true` を指定できる。ワークスペース外への書き込み権限は別途確認する。

## 時刻を指定して送る

利用上限の回復時刻などに合わせるなら、送信処理をその時刻に実行する。Claude Code のセッション内で予定を入れる場合は、利用できる環境で `CronCreate` を一度限り（`recurring: false`）に設定する。セッションの終了や Mac のスリープ中の実行には制約があり、この環境では未実測。[Claude Code の予約実行](https://code.claude.com/docs/en/scheduled-tasks)

セッション外から送る場合は、利用環境の一度限りの予約機能を使う。Claude Desktop の予約タスクや、Mac の `launchd` ジョブが候補になる。`launchd` で作ったジョブは送信後にそのジョブだけ `launchctl bootout` で外す。予約した権限モードが実行時にも使われるか、利用上限で止まった宛先が回復後に自動で受信するかは未確認。予約の実行結果と宛先への到達はそれぞれ確かめる。
