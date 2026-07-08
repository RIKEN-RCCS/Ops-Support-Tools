# Zendesk OAuth Tools

Zendesk Support API token を使わず、OAuth だけで token bundle を作成、refresh、疎通確認するための素の CLI ツールです。

このツールは `zendesk-support-ai` の webhook / worker には組み込まず、移行検証用に単独で実行します。

## Files

秘密値は `.env` へ書かず、`secrets/` 配下のファイルに保存します。

```text
apps/zendesk-oauth-tools/secrets/zendesk_oauth_client_secret
apps/zendesk-oauth-tools/secrets/zendesk_oauth_token_bundle.json
```

`secrets/` は root `.gitignore` で除外されています。

## Configuration

```bash
cp apps/zendesk-oauth-tools/.env.example apps/zendesk-oauth-tools/.env
```

`.env` には secret の値ではなく、URL、OAuth client identifier、redirect URI、secret file path だけを書きます。

```text
ZENDESK_URL=https://r-ccs.zendesk.com
ZENDESK_OAUTH_CLIENT_IDENTIFIER=ops-support-tools_zendesk_api
ZENDESK_OAUTH_CLIENT_SECRET_FILE=apps/zendesk-oauth-tools/secrets/zendesk_oauth_client_secret
ZENDESK_OAUTH_REDIRECT_URI=http://localhost
ZENDESK_OAUTH_TOKEN_BUNDLE_FILE=apps/zendesk-oauth-tools/secrets/zendesk_oauth_token_bundle.json
ZENDESK_OAUTH_SCOPES=read write
```

Zendesk の OAuth client 側にも、同じ redirect URI を登録しておきます。`http://localhost` を使う場合、ブラウザのリダイレクト先は接続失敗になって構いません。

認可後にブラウザで以下のような表示になるのは想定内です。

```text
localhost 接続が拒否されました
```

この時点でアドレスバーが `http://localhost/?code=...&state=...` のようになっていれば、認可自体は成功しています。`code=` から `&state=` の手前までをコピーして次の `exchange-code` に渡します。code は短命の認証情報なので、チャットやチケットには貼らないでください。

`invalid_request "redirect_uri" mismatch` が出る場合は、Zendesk 管理画面の OAuth client に登録したリダイレクト URL と `ZENDESK_OAUTH_REDIRECT_URI` を完全一致させます。以下は別の値として扱われます。

```text
http://localhost
http://localhost/
http://127.0.0.1
http://localhost:8080
```

## Usage

OAuth 認可 URL を作ります。出力された URL をブラウザで開き、Zendesk にログインして許可します。

```bash
python3 apps/zendesk-oauth-tools/zendesk_oauth_tool.py --env-file apps/zendesk-oauth-tools/.env authorize-url
```

出力には、実際に送信される `redirect_uri` も表示されます。この値を Zendesk 管理画面側のリダイレクト URL と照合します。

リダイレクト URL に含まれる `code` を交換し、token bundle JSON を保存します。

```bash
python3 apps/zendesk-oauth-tools/zendesk_oauth_tool.py --env-file apps/zendesk-oauth-tools/.env exchange-code --code 'copied-code'
```

保存済み bundle の metadata だけを表示します。token 値は表示しません。

```bash
python3 apps/zendesk-oauth-tools/zendesk_oauth_tool.py --env-file apps/zendesk-oauth-tools/.env inspect
```

保存済み bundle の access token で Zendesk API に疎通確認します。

```bash
python3 apps/zendesk-oauth-tools/zendesk_oauth_tool.py --env-file apps/zendesk-oauth-tools/.env check
```

refresh_token grant を明示的に試します。成功した場合は bundle JSON を更新します。

```bash
python3 apps/zendesk-oauth-tools/zendesk_oauth_tool.py --env-file apps/zendesk-oauth-tools/.env refresh
```

## Notes

- 旧 API token は使いません。
- `authorize-url` は `/oauth/authorizations/new` の URL を表示します。
- `exchange-code` は `/oauth/tokens` の `authorization_code` grant を使います。
- `check` は `Authorization: Bearer <access_token>` を使います。
- `refresh` は `/oauth/tokens` の `refresh_token` grant を使います。
- どのコマンドも token / refresh_token / client secret の値は標準出力に出しません。
- `invalid_grant` が返る場合は、code の有効期限切れ、redirect URI 不一致、client identifier 不一致、client secret 不一致、または refresh token 期限切れを疑います。
