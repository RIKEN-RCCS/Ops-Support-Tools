# zendesk_rccs

`apps/zendesk-support-ai` から R-CCS Zendesk API を使うための OAuth 中継サーバです。

Support AI worker には Zendesk OAuth access token / refresh token を渡しません。worker は Docker 内部ネットワークでこの relay に Bearer token を付けてアクセスし、relay だけが Zendesk OAuth token bundle を保持・更新します。

## Role

```text
zendesk-support-ai worker
  -> http://zendesk-rccs:8080/api/v2/...
  -> relay token check
  -> OAuth refresh if needed
  -> R-CCS Zendesk API
```

OAuth refresh は次の2経路で実行します。

- Zendesk API 呼び出し前に `expires_at` を確認し、期限が近ければ refresh
- `ZENDESK_RCCS_PERIODIC_REFRESH_SECONDS` ごとに期限確認し、必要なら refresh

Zendesk の refresh token rotation を想定し、`access_token` と `refresh_token` を含む token bundle 全体をファイルロック付きで原子的に上書きします。

## Files

秘密値と更新される token bundle は Git に入れません。

```text
apps/zendesk_rccs/.env
apps/zendesk_rccs/oauth.env
apps/zendesk_rccs/secrets/zendesk_rccs_oauth_client_secret
apps/zendesk_rccs/secrets/zendesk_rccs_relay_token
apps/zendesk_rccs/data/zendesk_oauth_token_bundle.json
apps/zendesk_rccs/data/events.sqlite
```

`apps/*/.env`、`apps/*/oauth.env`、`apps/*/secrets/`、`apps/*/data/` は root `.gitignore` で除外されています。

## Configuration

```bash
cp apps/zendesk_rccs/.env.example apps/zendesk_rccs/.env
```

`apps/zendesk_rccs/.env` には secret の値を書かず、file path と非秘密設定だけを書きます。

```text
ZENDESK_RCCS_URL=https://r-ccs.zendesk.com
ZENDESK_RCCS_OAUTH_CLIENT_IDENTIFIER=ops-support-tools_zendesk_api
ZENDESK_RCCS_OAUTH_CLIENT_SECRET_FILE=/run/secrets/zendesk_rccs_oauth_client_secret
ZENDESK_RCCS_OAUTH_TOKEN_BUNDLE_FILE=/data/zendesk_oauth_token_bundle.json
ZENDESK_RCCS_RELAY_TOKEN_FILE=/run/secrets/zendesk_rccs_relay_token
```

初回の OAuth token bundle は `apps/zendesk-oauth-tools/` の authorization code flow で作成し、`apps/zendesk_rccs/data/zendesk_oauth_token_bundle.json` に配置します。

`apps/zendesk-oauth-tools/.env` を一時的に R-CCS 用へ向ける場合は、少なくとも次の値を R-CCS 用にします。

```text
ZENDESK_URL=https://r-ccs.zendesk.com
ZENDESK_OAUTH_CLIENT_IDENTIFIER=<R-CCS OAuth client identifier>
ZENDESK_OAUTH_CLIENT_SECRET_FILE=apps/zendesk_rccs/secrets/zendesk_rccs_oauth_client_secret
ZENDESK_OAUTH_TOKEN_BUNDLE_FILE=apps/zendesk_rccs/data/zendesk_oauth_token_bundle.json
ZENDESK_OAUTH_SCOPES=read write
```

Docker ではコンテナ内の実行ユーザーを `10004:1000` にしています。token bundle と SQLite DB を更新できるよう、起動前に `data/` をこのユーザーが書ける状態にします。

```bash
mkdir -p apps/zendesk_rccs/data apps/zendesk_rccs/secrets
sudo chown -R 10004:1000 apps/zendesk_rccs/data
chmod 640 apps/zendesk_rccs/secrets/zendesk_rccs_oauth_client_secret
chmod 640 apps/zendesk_rccs/secrets/zendesk_rccs_relay_token
```

## Endpoints

この relay は support-ai 内部向けです。nginx へ公開する必要はありません。

```text
GET  /health
GET  /events?limit=50
ANY  /api/v2/...
```

`/api/v2/...` は relay token 認証後、同じ path / query / body で Zendesk API へ転送します。`/events` は直近の proxy 状態を確認するための運用口で、本文や token は返しません。

## Deployment

Compose service は `zendesk-rccs` です。ホスト確認用に `127.0.0.1:18380` に bind します。

```bash
docker compose build zendesk-rccs
docker compose up -d zendesk-rccs
```

`apps/zendesk-support-ai` の Docker services は `ZENDESK_RELAY_URL=http://zendesk-rccs:8080` と `ZENDESK_RELAY_TOKEN_FILE=/run/secrets/zendesk_rccs_relay_token` を受け取り、この relay 経由で Zendesk API を使います。

API token 認証用の `ZENDESK_KEY_FILE` は Compose の support-ai service には渡しません。
