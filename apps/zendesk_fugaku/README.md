# zendesk_fugaku

`fugaku.zendesk.com` 向けの外部連携用 Zendesk API 中継サーバです。

Zendesk の OAuth token はこの中継サーバだけが保持します。外部連携システムや監視システムには Zendesk token を共有せず、中継サーバ用の限定 Bearer token で認証します。

## Role

```text
external integration / monitoring system
  -> zendesk_fugaku
  -> Bearer token check
  -> OAuth refresh if needed
  -> fugaku.zendesk.com API
```

OAuth refresh は次の2経路で実行します。

- 外部連携 endpoint 処理時: Zendesk API 呼び出し前に `expires_at` を確認し、期限が近ければ refresh
- 定期 refresh: `ZENDESK_FUGAKU_PERIODIC_REFRESH_SECONDS` ごとに期限確認し、必要なら refresh

refresh token rotation を想定し、`access_token` と `refresh_token` を含む token bundle 全体をファイルロック付きで原子的に上書きします。

## Files

秘密値と更新される token bundle は Git に入れません。

```text
apps/zendesk_fugaku/.env
apps/zendesk_fugaku/oauth.env
apps/zendesk_fugaku/secrets/zendesk_fugaku_oauth_client_secret
apps/zendesk_fugaku/secrets/zendesk_fugaku_relay_token
apps/zendesk_fugaku/data/zendesk_oauth_token_bundle.json
apps/zendesk_fugaku/data/events.sqlite
```

`apps/*/.env`、`apps/*/oauth.env`、`apps/*/secrets/`、`apps/*/data/` は root `.gitignore` で除外されています。

Git に入れてよいのは、実値を含まない template、実装、README、Compose 定義だけです。

## Configuration

```bash
cp apps/zendesk_fugaku/.env.example apps/zendesk_fugaku/.env
```

`apps/zendesk_fugaku/.env` には secret の値を書かず、file path だけを書きます。

```text
ZENDESK_FUGAKU_URL=https://fugaku.zendesk.com
ZENDESK_FUGAKU_OAUTH_CLIENT_IDENTIFIER=ops-support-tools_fugaku_relay
ZENDESK_FUGAKU_OAUTH_CLIENT_SECRET_FILE=/run/secrets/zendesk_fugaku_oauth_client_secret
ZENDESK_FUGAKU_OAUTH_TOKEN_BUNDLE_FILE=/data/zendesk_oauth_token_bundle.json
ZENDESK_FUGAKU_RELAY_TOKEN_FILE=/run/secrets/zendesk_fugaku_relay_token
```

初回の OAuth token bundle は `apps/zendesk-oauth-tools/` と同じ authorization code flow で作成し、`apps/zendesk_fugaku/data/zendesk_oauth_token_bundle.json` に配置します。`apps/zendesk-oauth-tools/.env` を一時的に Fugaku 用へ向ける場合は、少なくとも次の値を Fugaku 用にします。

```text
ZENDESK_URL=https://fugaku.zendesk.com
ZENDESK_OAUTH_CLIENT_IDENTIFIER=<fugaku OAuth client identifier>
ZENDESK_OAUTH_CLIENT_SECRET_FILE=apps/zendesk_fugaku/secrets/zendesk_fugaku_oauth_client_secret
ZENDESK_OAUTH_TOKEN_BUNDLE_FILE=apps/zendesk_fugaku/data/zendesk_oauth_token_bundle.json
ZENDESK_OAUTH_SCOPES=read write
```

Docker ではコンテナ内の実行ユーザーを `10003:1000` にしています。token bundle と SQLite DB を更新できるよう、起動前に `data/` をこのユーザーが書ける状態にします。

```bash
mkdir -p apps/zendesk_fugaku/data apps/zendesk_fugaku/secrets
sudo chown -R 10003:1000 apps/zendesk_fugaku/data
chmod 640 apps/zendesk_fugaku/secrets/zendesk_fugaku_oauth_client_secret
chmod 640 apps/zendesk_fugaku/secrets/zendesk_fugaku_relay_token
```

## Endpoints

外向きの nginx URL は `/zendesk_fugaku` prefix 付きです。コンテナ内部の Flask endpoint は prefix なしです。

`/zendesk_fugaku/api/v2/` は外部連携元/監視システムの送信元IP制限と Bearer token 認証の両方を通った場合だけ Zendesk へ転送します。

旧 `/zendesk_fugaku/vendor/alerts` は廃止済みです。

### Health

```text
GET /health
```

外向き:

```text
GET https://fncx.r-ccs.riken.jp/zendesk_fugaku/health
```

### Requester Search

```text
GET /api/v2/search.json?query=accounts:<富岳ユーザID>
Authorization: Bearer <relay-token>
```

外向き:

```text
GET https://fncx.r-ccs.riken.jp/zendesk_fugaku/api/v2/search.json?query=accounts:<富岳ユーザID>
```

### Organization Search

```text
GET /api/v2/organizations/search.json?name=<課題ID>
Authorization: Bearer <relay-token>
```

外向き:

```text
GET https://fncx.r-ccs.riken.jp/zendesk_fugaku/api/v2/organizations/search.json?name=<課題ID>
```

### Ticket Status Search

```text
GET /api/v2/search.json?query=ticket_id:<チケット番号>
Authorization: Bearer <relay-token>
```

外向き:

```text
GET https://fncx.r-ccs.riken.jp/zendesk_fugaku/api/v2/search.json?query=ticket_id:<チケット番号>
```

### Ticket Create

```text
POST /api/v2/tickets.json
Authorization: Bearer <relay-token>
Content-Type: application/json
```

request body は Zendesk の `POST /api/v2/tickets.json` に渡すJSONと同じです。

外向き:

```text
POST https://fncx.r-ccs.riken.jp/zendesk_fugaku/api/v2/tickets.json
```

### Ticket Comment / Update

```text
PUT /api/v2/tickets/<チケット番号>.json
Authorization: Bearer <relay-token>
Content-Type: application/json
```

連携元が `curl -d` の既定で `POST` する場合も、中継サーバ内で Zendesk の ticket update 用 `PUT` として転送します。

外向き:

```text
PUT https://fncx.r-ccs.riken.jp/zendesk_fugaku/api/v2/tickets/<チケット番号>.json
POST https://fncx.r-ccs.riken.jp/zendesk_fugaku/api/v2/tickets/<チケット番号>.json
```

### Upload

```text
POST /api/v2/uploads.json?filename=<ファイル名>
Authorization: Bearer <relay-token>
Content-Type: application/binary
```

request body は Zendesk の upload API にそのまま転送します。

外向き:

```text
POST https://fncx.r-ccs.riken.jp/zendesk_fugaku/api/v2/uploads.json?filename=<ファイル名>
```

### Recent Events

```text
GET /events?limit=50
Authorization: Bearer <relay-token>
```

直近の処理状態を確認します。本文や token は返さず、`event_id`、`source`、`status`、Zendesk ticket id、時刻、短い error だけを返します。`event_id` は `Idempotency-Key`、`X-Request-Id`、`X-Event-Id` のいずれかがあればそれを使い、なければ中継サーバが生成します。

外向き:

```text
GET https://fncx.r-ccs.riken.jp/zendesk_fugaku/events?limit=50
```

## Deployment

Compose service は `zendesk-fugaku` です。ホストでは `127.0.0.1:18280` に bind します。

```bash
docker compose build zendesk-fugaku
docker compose up -d zendesk-fugaku
```

nginx では `/zendesk_fugaku/api/v2/` など必要最小限の path だけを公開し、外部連携元/監視システムの送信元IPで制限してください。

例:

```text
/zendesk_fugaku/api/v2/ -> http://127.0.0.1:18280/api/v2/
```

OAuth refresh は定期refreshと、Zendesk API 呼び出し前の期限確認でアプリ内部だけが実行します。refresh専用の外部endpointは持ちません。

`/events` も運用確認用なので、外部公開する場合は連携元/運用者IPに絞ってください。
