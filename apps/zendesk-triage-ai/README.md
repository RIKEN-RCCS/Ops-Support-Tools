# Zendesk Triage with AI

Zendesk の新規チケットを OpenAI 互換 LLM で一次トリアージし、結果を Zendesk の内部メモとして書き戻す小さなパイプラインです。

入口は polling と webhook の両方を用意しています。どちらの入口でも `incoming/` に ticket id を積み、後段の `generator.py` と `poster.py` が同じ処理を行います。

## Architecture

| File | Role |
|---|---|
| `webhook.py` | Zendesk webhook を受け、ticket id を `incoming/` に積む |
| `poller.py` | Zendesk Search API を polling し、ticket id を `incoming/` に積む |
| `generator.py` | チケット本文取得、PII マスキング、LLM 生成、`pending/` 出力 |
| `poster.py` | 検証、unmask、内部メモ投稿、タグ付与、任意の担当者フィールド更新 |
| `common.py` | 設定、スプール管理、Zendesk API クライアント |
| `pii_mask.py` | メール、IP、電話番号、アカウント ID などの機械的マスキング |
| `llm_client.py` | OpenAI 互換 Chat Completions API 呼び出し |
| `agents.example.json` | 担当候補設定のテンプレート |
| `sync_agents.py` | Zendesk から light agent 一覧を同期 |
| `Dockerfile` | コンテナ image |

## Safety Model

- LLM に渡すのは allowlist 抽出した subject/comment body のみです。
- メール、IP、電話番号、ホームディレクトリ内ユーザー名、アカウント ID は LLM 送信前にマスクします。
- Zendesk に書き込むのは `poster.py` だけです。
- 書き込み操作は内部メモ追加、タグ追加、任意のカスタムフィールド更新に限定しています。
- AI は担当者 ID を出しません。担当者は `agents.json` と決定論的ロジックで選びます。
- ラウンドロビン対象の `light_agents` は Zendesk から同期します。
- `agents.json`、`.env`、`spool/` は Git に入れない前提です。

## Configuration

`.env.example` を元に `.env` を作成します。

| Variable | Purpose |
|---|---|
| `TRIAGE_SPOOL_DIR` | スプール保存先 |
| `TRIAGE_TAG` | 投稿済み判定に使う Zendesk tag |
| `TRIAGE_SEARCH_QUERY` | polling 用 Zendesk Search query |
| `TRIAGE_WEBHOOK_TOKEN` | webhook 共有トークン。未設定なら認証チェックなし |
| `LLM_BASE_URL` / `LLM_API_KEY` | OpenAI 互換 LLM endpoint |
| `TRIAGE_MODEL` | 使用モデル |
| `TRIAGE_FALLBACK_MODELS` | フォールバックモデル。カンマ区切り |
| `TRIAGE_SUPPORT_CONTEXT` | LLM に渡すサポート対象の説明 |
| `ZENDESK_URL` / `ZENDESK_EMAIL` / `ZENDESK_KEY` | Zendesk API token 認証 |
| `TRIAGE_AGENTS_FILE` | 担当者名簿 JSON |
| `TRIAGE_STARTUP_CHECKS` | コンテナ起動時に preflight を実行する |
| `TRIAGE_STARTUP_RETRIES` | Zendesk/LLM 疎通確認のリトライ回数 |
| `TRIAGE_SYNC_AGENTS_ON_STARTUP` | 起動時に Zendesk から `light_agents` を同期する |
| `TRIAGE_SYNC_AGENTS_ON_POST` | `poster.py` 実行時に Zendesk から `light_agents` を同期する |
| `TRIAGE_LIGHT_AGENT_ROLE_TYPE` | light agent 判定に使う Zendesk `role_type`。既定 `1` |
| `TRIAGE_LLM_HEALTHCHECK_PATH` | LLM 疎通確認 endpoint。既定 `/models` |

担当者フィールドを使う場合は `agents.example.json` を `agents.json` にコピーし、Zendesk の ticket custom field id を `assignee_field_id` に設定します。`light_agents` は `sync_agents.py` または `poster.py` が Zendesk から更新します。

`escalation_map` は user id ではなく人名または email で書けます。値は単一値またはリストにできます。

```json
{
  "escalation_map": {
    "scheduler": ["Example Agent A"],
    "storage": ["storage-owner@example.com"],
    "other": null
  }
}
```

同期だけ手動で確認する場合:

```bash
python3 sync_agents.py --dry-run
python3 sync_agents.py
```

## Startup Checks

Docker では [`startup.py`](startup.py) が entrypoint です。各サービスの起動時と再起動時に次を確認してから本体プロセスへ切り替わります。

- `TRIAGE_SPOOL_DIR` のディレクトリ作成
- `TRIAGE_AGENTS_FILE` が無ければ `agents.example.json` から初期生成
- Zendesk API token の疎通確認
- LLM endpoint の疎通確認
- Zendesk から light agent 一覧を取得して `agents.json` を更新

疎通確認に失敗した場合は起動に失敗します。Compose の `restart: unless-stopped` により、設定や外部サービスが復旧すると再試行されます。

## Webhook Mode

Zendesk webhook の送信先を次に設定します。

```text
POST https://your-host.example.com/zendesk/webhook
```

payload は次のいずれかの形なら受け付けます。

```json
{"ticket_id": 12345}
```

```json
{"ticket": {"id": 12345}}
```

`TRIAGE_WEBHOOK_TOKEN` を設定した場合は、Zendesk webhook 側で次のどちらかのヘッダーを付けます。

```text
Authorization: Bearer replace-me
X-Triage-Webhook-Token: replace-me
```

## Docker

```bash
# リポジトリ root で実行
cp .env.example .env
docker compose up --build
```

リポジトリ root の `docker-compose.yml` は `webhook`、`generator`、`poster` の 3 サービスを非 root ユーザーで起動し、名前付き volume `triage-spool` と `triage-config` を共有します。`triage-config` には初回作成時に `agents.example.json` 由来の `/config/agents.json` が入り、同期で更新されます。

`escalation_map` を編集する場合は、volume 内の `/config/agents.json` を編集します。ホスト bind mount にしたい場合は、コンテナの `triage` ユーザー(uid `10001`)が書けるディレクトリを `/config` に mount してください。

まず安全確認したい場合は `docker-compose.yml` の `poster` command に `--dry-run` を追加してください。

## Polling Mode

Webhook を使わず polling で動かす場合:

```bash
python3 poller.py --once -v
python3 generator.py --once -v
python3 poster.py --once -v --dry-run
python3 poster.py --once -v
```

## Tests

```bash
python3 test_pii_mask.py
python3 test_assignment.py
```
