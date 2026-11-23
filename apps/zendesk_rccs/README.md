# zendesk_rccs

`apps/zendesk-support-ai` から サポート用Zendesk API を使うための OAuth 中継サーバです。

Support AI worker には Zendesk OAuth access token / refresh token を渡しません。worker は Docker 内部ネットワークでこの relay に Bearer token を付けてアクセスし、relay だけが Zendesk OAuth token bundle を保持・更新します。

## Role

```text
zendesk-support-ai worker
  -> http://zendesk-rccs:8080/api/v2/...
  -> relay token check
  -> OAuth refresh if needed
  -> サポート用Zendesk API
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
ZENDESK_RCCS_URL=https://your-subdomain.zendesk.com
ZENDESK_RCCS_OAUTH_CLIENT_IDENTIFIER=replace-with-client-identifier
ZENDESK_RCCS_OAUTH_CLIENT_SECRET_FILE=/run/secrets/zendesk_rccs_oauth_client_secret
ZENDESK_RCCS_OAUTH_TOKEN_BUNDLE_FILE=/data/zendesk_oauth_token_bundle.json
ZENDESK_RCCS_RELAY_TOKEN_FILE=/run/secrets/zendesk_rccs_relay_token
```

初回の OAuth token bundle は `apps/zendesk-oauth-tools/` の authorization code flow で作成し、`apps/zendesk_rccs/data/zendesk_oauth_token_bundle.json` に配置します。

`apps/zendesk-oauth-tools/.env` を一時的に サポート用へ向ける場合は、少なくとも次の値を サポート用にします。

```text
ZENDESK_URL=https://your-subdomain.zendesk.com
ZENDESK_OAUTH_CLIENT_IDENTIFIER=<OAuth client identifier>
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

## Optional client API restrictions

Set `ZENDESK_RCCS_CLIENTS_FILE` to an absolute JSON file path to enable
client-specific permissions. Missing, invalid or empty policy files fail startup.
When enabled, the shared relay token is not accepted. When unset, existing shared
relay authentication remains available for compatibility.

Each JSON member is a client label and has `token_file` (an absolute path to a
unique secret containing at least 32 ASCII letters, digits, underscores or
hyphens) and `profile`. Never commit real policy files or tokens.

| Profile | Allowed operations |
| --- | --- |
| `read` | Ticket/comments, agent directory, groups, search and current-user GET |
| `internal-note` | Reads above; ticket PUT with explicit private comment and optional tags |
| `triage` | Reads/private notes; allowlisted custom fields, assignees and groups |
| `public-reply` | Current-user/ticket/comments GET; explicit public comment, tags and permitted status |
| `audit` | Relay `/events` GET only |

Assignment IDs use optional positive-integer arrays `custom_field_ids`,
`assignee_ids` and `group_ids`; unspecified arrays grant no assignment permission.
`public_statuses` defaults to `["pending"]`; allowed values are `new`, `open`,
`pending`, `hold`. Solving/closing is excluded. Ticket creation, uploads,
user/organization writes, deletion and PATCH/POST update aliases are excluded.
JSON payloads reject unlisted fields, comment authors, attachments and implicit
comment visibility. Pagination parameters and repeated `role[]` values are
preserved. Scoped requests never follow upstream redirects.

All routes check the incoming method/path before OAuth or forwarding. Audit
identifiers are namespaced by client; they do not provide posting deduplication.
Upstream exception bodies are not persisted in scoped error records. Network
errors are not retried; an explicit upstream 401 retains the existing single
OAuth refresh retry. This policy does not reduce the upstream OAuth account's
permissions or limit reads/writes to a particular ticket. Possession of a public
reply token grants that operation; it does not prove human approval.

Run the offline regression with `python -B apps/zendesk_rccs/test_client_policy.py`
from a checkout with the relay dependencies installed. It uses synthetic secrets,
a temporary database and mocked HTTP only; it never contacts Zendesk.
