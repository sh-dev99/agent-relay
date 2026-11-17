# 接続にひと手間かかるアプリについて

Muse と Grok Bot は、スキルを入れるだけでは他の AI とやり取りできません。追加で何を用意すればアプリの枠を越えてメッセージを届けられるのか、それぞれの仕組みをまとめました。自分の環境に導入したいときは、この説明も AI に読ませてみてください。

## Muse

Muse のメイン会話にメッセージを届けるため、Mac 上で動く専用のブリッジツールを作りました。このリポジトリには含まれていませんが、仕組みは次のとおりです。

1. 送信する AI が、依頼文と返信先を書いたファイルを Mac の受信用フォルダに置く。
2. 常駐しているブリッジがファイルを見つけ、Muse のメイン会話へ届ける。
3. Muse が返信を書くと、ブリッジが送り先に合った方法で相手の AI へ返す。

Muse への接続には、外部の機器を Muse につなぐ「Muse Gadget」の仕組みを使っています。Mac をその機器として登録するのに使ったのが、非公式の互換クライアント [muse-client](https://github.com/wong2/muse-client) です。

初回登録では、スマートフォンの Muse アプリと Mac を Bluetooth でペアリングします。Bluetooth は登録のために使うもので、登録後のメッセージはインターネット経由の WebSocket 接続で送ります。

再現するには、Muse アプリ、スマートフォンでの機器登録、Mac 上のクライアントとブリッジが必要です。ブリッジは今後公開する予定なので、それまでは同様の処理を自分で用意する必要があります。[muse-client の登録手順](https://github.com/wong2/muse-client#pair-with-muse)と、元になる [Meta の Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk) が参考になります。

## Grok Bot

Grok Bot には、きっかけに応じて決めた作業を始める「Routine」があります。そのきっかけに、外部から呼び出せる通知先「Webhook」を使います。

1. 送信する AI が、依頼文と返信先を Mac 上のファイルに保存する。
2. 同梱の送信スクリプトが Webhook を呼び出し、Routine を起動する。
3. Bot が Mac 上のファイルを読み、依頼に対応する。返信も Mac 上の agent-relay を使って相手へ送る。

用意するのは、Webhook で起動する Routine と、Bot から Mac 上のコマンドを実行できる接続です。Mac 側の接続は「Settings → Computer → Your computer → Execution on this computer」で設定します（[公式の説明](https://cursor.com/help/grok-bot/faqs)）。Muse と違って送信スクリプトはこのリポジトリに入っていますが、Routine や接続の設定は各自の環境で行います。

Bot には「Webhook で起動したら Mac の依頼ファイルを読んで対応し、返信先があれば返して」と頼んで Routine を作ります。Webhook の URL とキーは、デスクトップのチャット上部にある Bot 名から「詳細」→「ルーティン」で確認できます。キーの保存方法や送信コマンドは [スキルの手順](../skills/agent-relay/SKILL.md)、Routine の設定は [Grok Bot 公式の説明](https://cursor.com/help/grok-bot/routines)を参照してください。

この方式では、依頼を読むために Mac 側の接続も必要です。また、依頼ファイルを次の送信で上書きするので、続けて送るときは前の依頼が読まれたことを確認してください。
