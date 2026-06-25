# Zendesk Support AI

Zendesk の新規チケット一次トリアージ、追加質問への返信ドラフト、担当者補助を扱う小さな AI support パイプラインです。結果は Zendesk の内部メモとして書き戻し、公開返信は人間の確認を前提にします。

入口は polling と webhook の両方を用意しています。どちらの入口でも SQLite queue に ticket id を積み、後段の worker が同じ処理を行います。

## Architecture

| File | Role |
|---|---|
| `webhook.py` | Zendesk webhook を受け、ticket id を `incoming/` に積む |
| `poller.py` | Zendesk Search API を polling し、ticket id を `incoming/` に積む |
| `generator.py` | チケット本文取得、PII マスキング、LLM 生成、`pending/` 出力 |
| `poster.py` | 検証、unmask、内部メモ投稿、タグ付与、任意の担当者フィールド更新 |
| `followup.py` | 追加質問 webhook の誤発火判定、追加質問候補の `pending_followup/` 振り分け |
| `followup_responder.py` | 追加質問への返信ドラフトを生成し、内部メモとして投稿 |
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
- `config/agents.json`、`.env`、`secrets/`、`spool/` は Git に入れない前提です。
- 本番 Docker では秘密値を `.env` / `env_file` で渡さず、Docker secrets ファイルとして渡します。

## Configuration

このアプリの `.env.example` を元に、同じディレクトリへ `.env` を作成します。Docker Compose 本番運用では `.env` に API key や webhook token を入れず、アプリ配下の `secrets/` に分離します。

| Variable | Purpose |
|---|---|
| `SUPPORT_AI_QUEUE_DIR` | SQLite queue とレガシースプールディレクトリの保存先 |
| `SUPPORT_AI_QUEUE_DB` | support AI worker のSQLite queue DB。既定は `$SUPPORT_AI_QUEUE_DIR/queue.sqlite` |
| `SUPPORT_AI_TRIAGE_TAG` | 投稿済み判定に使う Zendesk tag |
| `SUPPORT_AI_TRIAGE_SEARCH_QUERY` | polling 用 Zendesk Search query |
| `SUPPORT_AI_KNOWLEDGE_API_URL` | Knowledge API の内部URL。設定時、環境確認が必要なtriageをrunへ送る |
| `SUPPORT_AI_CREATE_KNOWLEDGE_RUNS` | `requires_runbook` / `requires_environment_knowledge` のtriageでKnowledge runを作る |
| `SUPPORT_AI_WEBHOOK_TOKEN` / `SUPPORT_AI_WEBHOOK_TOKEN_FILE` | webhook 共有トークン。未設定なら認証チェックなし |
| `SUPPORT_AI_QUEUE_KEY` / `SUPPORT_AI_QUEUE_KEY_FILE` | スプール JSON 暗号化キー。Docker では secrets/support_ai_queue_key を使う |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_API_KEY_FILE` | OpenAI 互換 LLM endpoint |
| `SUPPORT_AI_MODEL` | 使用モデル |
| `SUPPORT_AI_FALLBACK_MODELS` | フォールバックモデル。カンマ区切り |
| `SUPPORT_AI_CONTEXT` | LLM に渡すサポート対象の説明 |
| `ZENDESK_URL` / `ZENDESK_EMAIL` / `ZENDESK_KEY` / `ZENDESK_KEY_FILE` | Zendesk API token 認証 |
| `SUPPORT_AI_AGENTS_FILE` | 担当者名簿 JSON |
| `SUPPORT_AI_STARTUP_CHECKS` | コンテナ起動時に preflight を実行する |
| `SUPPORT_AI_STARTUP_RETRIES` | Zendesk/LLM 疎通確認のリトライ回数 |
| `SUPPORT_AI_SYNC_AGENTS_ON_STARTUP` | 起動時に Zendesk から `light_agents` を同期する |
| `SUPPORT_AI_SYNC_AGENTS_ON_POST` | `poster.py` 実行時に Zendesk から `light_agents` を同期する |
| `SUPPORT_AI_LIGHT_AGENT_ROLE_TYPE` | light agent 判定に使う Zendesk `role_type`。既定 `1` |
| `SUPPORT_AI_ASSIGNEE_FIELD_ID` | Zendesk の「対応担当」ticket custom field id。未設定だと担当者フィールドを書き込まない |
| `SUPPORT_AI_LLM_HEALTHCHECK_PATH` | LLM 疎通確認 endpoint。既定 `/models` |

`SUPPORT_AI_ASSIGNEE_FIELD_ID` は担当者自動設定に必須です。Zendesk の管理画面で対象の ticket custom field id を確認し、環境ごとの `.env` に設定してください。

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

- `SUPPORT_AI_QUEUE_DIR` のディレクトリ作成
- `SUPPORT_AI_AGENTS_FILE` が無ければ `agents.example.json` から初期生成
- Zendesk API token の疎通確認
- LLM endpoint の疎通確認
- Zendesk から light agent 一覧を取得して `agents.json` を更新

疎通確認に失敗した場合は起動に失敗します。Compose の `restart: unless-stopped` により、設定や外部サービスが復旧すると再試行されます。

## Webhook Mode

トリアージ用 Zendesk webhook の送信先を次に設定します。

```text
POST https://your-host.example.com/zendesk/webhook/triage
```

追加質問/追記コメントを別ジョブとして受ける場合は、次を使います。

```text
POST https://your-host.example.com/zendesk/webhook/followup
```

`followup` は `incoming_followup/` に積まれます。`followup.py` が最新の公開エンドユーザーコメントを確認し、追加質問らしい場合は `pending_followup/` に回します。追加質問らしいコメントが見つからない場合は、チケットへ内部メモを投稿して trigger 条件の見直しを促します。`followup_responder.py` は `pending_followup/` を消費し、返信ドラフトを内部メモとして投稿します。公開返信は行いません。

payload は次のいずれかの形なら受け付けます。

```json
{"ticket_id": 12345}
```

```json
{"ticket": {"id": 12345}}
```

`SUPPORT_AI_WEBHOOK_TOKEN` を設定した場合は、Zendesk webhook 側で次のどちらかのヘッダーを付けます。

```text
Authorization: Bearer replace-me
X-Support-AI-Webhook-Token: replace-me
```

## Triage Gate and Runbook Handoff

Triage AI は一次返信案だけでなく、公開返信へ進めてよいかを判定します。出力には次のゲート項目が含まれます。

```text
requires_environment_knowledge
requires_runbook
requires_operator_check
safe_to_reply_to_user
answer_confidence
suggested_next_action
```

`safe_to_reply_to_user=false` の場合、社内メモでは一次返信ドラフトを「公開返信への利用は保留」として表示します。環境固有の module、CUDA、MPI、コンパイラ、ストレージ、ジョブ実行環境、サポート範囲などが必要な問い合わせでは、ユーザーへ一般論を返す前に担当者が Knowledge/runbook で確認します。

`SUPPORT_AI_KNOWLEDGE_API_URL` が設定され、`SUPPORT_AI_CREATE_KNOWLEDGE_RUNS=1` の場合、`requires_runbook=true` または `requires_environment_knowledge=true` のtriageは Knowledge API に `status=requested` の run を作ります。run には初期runbookとして、既存知見確認、実機確認、リスク評価、findings / answer draft 登録の手順が入ります。同じ `ticket_id` に未完了の `requested` run が既にある場合は、runbook decision agent が「既存runへ文脈を attach するだけでよいか」「より深く/広く/作り直しの新規調査runが必要か」「runbook不要か」「担当者判断へ戻すか」を判定します。判定結果は `runbook-decision` document として Knowledge に残します。attach は同じrunbookの再実行を意味しません。

Followup responder も同じゲートと decision agent を使います。公開会話履歴を LLM に渡すときは Zendesk user id / author_id を渡さず、`speaker=end_user` または `speaker=support` のみを使います。decision agent の `runbook_change` は `none`、`append_context`、`deepen`、`broaden`、`replace`、`initial` のいずれかです。

## Monitor

`webhook` service also provides a small queue monitor for operators:

```text
http://127.0.0.1:18080/support-ai/monitor
http://127.0.0.1:18080/support-ai/monitor/incoming
http://127.0.0.1:18080/support-ai/monitor/incoming_followup
http://127.0.0.1:18080/support-ai/monitor/pending
http://127.0.0.1:18080/support-ai/monitor/pending_followup
http://127.0.0.1:18080/support-ai/monitor/done
http://127.0.0.1:18080/support-ai/monitor/failed
```

If `SUPPORT_AI_WEBHOOK_TOKEN` is configured, the monitor uses the same token. In a browser, use Basic authentication with any username and the token as the password.

## Docker

```bash
# リポジトリ root で実行
cp apps/zendesk-support-ai/.env.example apps/zendesk-support-ai/.env
mkdir -p apps/zendesk-support-ai/secrets
# API key/token は .env ではなく次のファイルに保存します。
# apps/zendesk-support-ai/secrets/llm_api_key
# apps/zendesk-support-ai/secrets/zendesk_key
# apps/zendesk-support-ai/secrets/support_ai_webhook_token
# apps/zendesk-support-ai/secrets/support_ai_queue_key
docker compose up --build
```

リポジトリ root の `docker-compose.yml` は `webhook`、`generator`、`poster`、`followup`、`followup-responder` を非 root ユーザーで起動します。SQLite queue は app-local bind mount `apps/zendesk-support-ai/data:/data/queue`、設定は `apps/zendesk-support-ai/config:/config` を共有します。`config/agents.json` は初回作成時に `agents.example.json` 由来で作られ、起動時同期で更新されます。

`docker-compose.yml` はアプリ配下の `.env` を非機微設定として読みます。これはアプリごとに設定を分けるためです。秘密値は `.env` ではなく Compose secrets として `/run/secrets/...` に mount され、アプリは `*_FILE` 変数から読み込みます。これにより `docker compose config` の誤実行で API key/token 本体が標準出力に展開される事故を防ぎます。

`apps/zendesk-support-ai/secrets/support_ai_webhook_token` が空の場合、従来通り webhook token 認証は無効になります。本番では必ず値を入れてください。

`apps/zendesk-support-ai/secrets/support_ai_queue_key` が設定されている場合、support AI worker の中間データは `SUPPORT_AI_QUEUE_DB` の SQLite `queue_items` テーブルへ入り、payload は AES-GCM で暗号化された JSON envelope として保存されます。`incoming`、`pending`、`pending_followup`、`done`、`failed` はテーブル上の queue 名です。既存の平文スプール JSON は互換のため読み取り可能ですが、新規書き込みは SQLite queue に入ります。キーを失うと暗号化済み queue payload は復号できないため、ローテーション前に未処理キューを空にしてください。

## Move / Restore

Server moves are intentionally app-local. Stop Compose, copy the repository plus these app-local runtime directories, then start Compose on the new server:

```text
apps/zendesk-support-ai/.env
apps/zendesk-support-ai/config/
apps/zendesk-support-ai/data/
apps/zendesk-support-ai/secrets/
apps/knowledge-api/data/
apps/knowledge-api/secrets/
```

The two most important keys are `apps/zendesk-support-ai/secrets/support_ai_queue_key` and `apps/knowledge-api/secrets/knowledge_field_key`. Losing them makes encrypted queue payloads or knowledge fields unrecoverable.

`escalation_map` を編集する場合は、ホスト側の `apps/zendesk-support-ai/config/agents.json` を編集します。Compose ではコンテナ UID `10001`、ホスト group `1000` で書けるように起動します。

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
