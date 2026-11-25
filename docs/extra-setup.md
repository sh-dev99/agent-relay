# Muse と Grok Bot の追加設定

Muse に送るには Mac 上のブリッジが必要です。Grok Bot に送るには、Webhook で起動する Routine を用意します。

## Muse

ブリッジは、Mac の受信用フォルダに置かれた依頼文を見つけると、Muse のメイン会話に届けます。返信先も渡しておけば、送り先に応じた方法で返信できます。このスキルには、受信用フォルダまで文を届ける送信スクリプトが入っています。

Mac を Muse に接続するには、外部の機器を登録する「Muse Gadget」の仕組みを使います。検証には非公式の互換クライアント `muse-client` を使いました。初回はスマートフォンの Muse アプリと Mac を Bluetooth でペアリングし、その後はインターネット経由の WebSocket で送受信します。[muse-client の登録手順](https://github.com/wong2/muse-client#pair-with-muse)・[Meta の Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk)

使うには、Muse アプリ、登録したクライアント、Mac 上で常駐するブリッジを用意し、送信スクリプトにブリッジの受信用フォルダを指定します。常駐ブリッジはこのリポジトリに含まれません。[Muse の送信手順](../skills/agent-relay/references/other-apps.md#muse)

## Grok Bot

Grok Bot の「Routine」は、指定したきっかけで作業を開始する機能です。ここでは、外から呼び出せる Webhook をきっかけにします。

1. Grok Bot の画面で Webhook をきっかけにする Routine を作り、受信した JSON の `request` を依頼本文として処理するよう設定します。
2. Routine の Webhook を確認し、送信側に URL とキーを設定します。設定方法は、同梱スクリプトの手順を参照してください。[Grok Bot の設定と送信手順](../skills/agent-relay/references/other-apps.md#grok-botxai)
3. 送信すると、Routine のチャットに処理の結果が出ます。別のエージェントへの返信も必要なら、依頼文に送り先を添え、Routine がその送り先へ返せるようにします。[Grok Bot の Routine](https://cursor.com/help/grok-bot/routines)

Mac 上でファイルを読んだり、返信用のコマンドを実行したりする場合は、Grok Bot を Mac に接続します。設定箇所は「Settings → Computer → Your computer → Execution on this computer」です。[Grok Bot の接続設定](https://cursor.com/help/grok-bot/faqs)

以前から Mac の依頼ファイルを使っている場合は、Webhook に `request` がないときだけファイルを読むように設定すれば、従来の依頼も受け取れます。ファイルは同じ場所に上書きされるので、この方法で受け取る場合は前の依頼が読まれたことを確認してから続けて送ってください。
