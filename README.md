# agent-relay

Claude Code に書いてもらった指示書を、そのまま Codex に渡して実装を頼みたい。そんなときは「この指示書、Codex のこのチャットに送って」と頼めます。agent-relay は、別の AI の会話へ文章を送るためのスキルです。

以前の会話に追加情報を渡したり、複数の AI が調べたことを一か所に集めたりするときにも使えます。返信先に元のチャットを指定すれば、実装結果を Claude Code に戻して、指示どおりに動くか確認してもらえます。会話の履歴やファイルは自動で共有されません。資料が必要な場合は、相手から読めるファイルのパスを本文に添えてください。

![Claude と Codex の会話例](assets/relay-example.png)

## 使ってみる

Python 3 と、使いたい AI ツールのインストール・ログインが必要です。macOS で動作を確認しています。Windows での対応状況は接続先によって異なり、未検証の経路があります。Grok Bot と Muse に送るには、追加の設定が必要です。[Muse と Grok Bot の追加設定](docs/extra-setup.md)

PC 上で作業できる AI にリポジトリの URL を渡せば、導入を頼めます。[agent-relay のリポジトリ](https://github.com/sh-dev99/agent-relay)

> このリポジトリを読んで、この PC で使えるように入れて。使いたい送り先は Claude Code と Codex。追加設定が必要なら、設定する場所も教えて。

<details>
<summary>手動で導入する</summary>

### Claude Code のプラグインで入れる

Claude Code の会話欄で順に実行します。

```text
/plugin marketplace add sh-dev99/agent-relay
/plugin install agent-relay@sh-dev99-agent-relay
```

### スキルフォルダをコピーする

このリポジトリをダウンロードし、そのフォルダをターミナルで開きます。同名のスキルがすでにある場合は、上書きの前に内容を確認してください。

Codex などでスキルを `~/.agents/skills` に置いている場合は、次を実行します。

```bash
mkdir -p ~/.agents/skills
cp -r skills/agent-relay ~/.agents/skills/
```

Claude Code 専用のスキルとして入れる場合は、次を実行します。

```bash
mkdir -p ~/.claude/skills
cp -r skills/agent-relay ~/.claude/skills/
```

別のスキル置き場を使っている場合は、その場所に `agent-relay` フォルダをコピーします。

</details>

導入後は「この内容を○○のチャットに送って」と頼みます。宛先の ID が分かるなら、名前と一緒に伝えてください。名前で検索できる経路でも、同名の会話がある場合は ID を指定します。Codex の一覧には、この端末のチャットと、別のホストのチャット（durable）が一緒に並ぶことがあります。このスキルの Codex 送信スクリプトが扱うのは、この端末のチャットです。

## 送受信を確認した組み合わせ

表は2026年11月14日と16日に、macOS の各アプリで確認した結果です。11月26日には、Codex と Antigravity の間でもう一度、両方向の送信を試しました。表の○は、相手の会話への到達または返答を確認した組み合わせを示します。

| 受け手 ＼ 送り手 | Claude Code | Codex | OpenCode | Cursor | Grok Bot | Muse | Antigravity |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Claude Code | ／ | ○ | ○ | ○ | ○ | ○ | ○ |
| Codex | ○ | ／ | ○ | ○ | ○ | ○ | ○ |
| OpenCode | ○ | ○ | ／ | ○ | ○ | ○ | ○ |
| Cursor | — | — | — | ／ | — | — | — |
| Grok Bot | ○ | ○ | ○ | ？ | ／ | ○ | ○ |
| Muse | ○ | ○ | ○ | ○ | ○ | ／ | ○ |
| Antigravity | ○ | ○ | ○ | ○ | ○ | ○ | ／ |

？は未確認、—はスキルに受信経路がない組み合わせ、／は同じ製品同士です。表には CLI 専用のセッションを含めていません。Cursor Desktop からは他のアプリへ送れます。外部から Cursor Desktop の既存会話に届ける経路は確認できていません。

## 相手が作業中の場合

ローカル Codex への通常送信は、公式のキューに入ります。実機で試したところ、Antigravity から送った2通が Codex のキューに表示され、ユーザーが「今すぐ反映」を押すと会話の本文に入りました。指定したターンへの追記が必要な場合は、宛先と利用できる経路を確認してください。[Codex の送信手順](skills/agent-relay/references/codex.md)

逆に Codex から Antigravity に続けて送った例では、Antigravity は1通目の待ち時間に2通目を読み、そちらに先に返信しました。子エージェントやバックグラウンドの作業が続いていると、配信が保留される場合もあります。[Antigravity の送信手順](skills/agent-relay/references/antigravity.md)

コマンドの成功表示は受付を示します。相手に届いたかは、宛先の会話記録や画面で確かめてください。返信が必要なら本文に返信先を添えます。[到達確認と再送の手順](skills/agent-relay/references/delivery.md)

## 新しい会話を作る

Antigravity では、`agy_send.py new` でアプリ会話を作れます。Codex Desktop では、利用環境から `create_thread` を使える場合に新しいチャットを作れます。

Claude Code のバックグラウンドセッション、Codex の共有デーモンや `exec`、OpenCode CLI にも、新規セッションを作る手段があります。これらは各 CLI のセッションです。Grok Bot 宛ての送信は、用意した Routine を起動します。[連携先ごとのコマンド](skills/agent-relay/SKILL.md)

## 更新履歴とライセンス

- [更新履歴](CHANGELOG.md)
- [MIT ライセンス](LICENSE)
