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
| `SUPPORT_AI_RUNBOOK_WORKER_ENABLED` | `status=requested` の Knowledge run から runbook plan を生成する worker を有効化 |
| `SUPPORT_AI_RUNBOOK_REVIEW_WORKER_ENABLED` | runbook plan の risk / technical / chief 評価 worker を有効化 |
| `SUPPORT_AI_RUNBOOK_MAX_REVISIONS` | chief review からの自動 revise 上限。既定 `2` |
| `SUPPORT_AI_WEBHOOK_TOKEN` / `SUPPORT_AI_WEBHOOK_TOKEN_FILE` | webhook 共有トークン。未設定なら認証チェックなし |
| `SUPPORT_AI_QUEUE_KEY` / `SUPPORT_AI_QUEUE_KEY_FILE` | スプール JSON 暗号化キー。Docker では secrets/support_ai_queue_key を使う |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_API_KEY_FILE` | OpenAI 互換 LLM endpoint |
| `SUPPORT_AI_MODEL` | 使用モデル |
| `SUPPORT_AI_FALLBACK_MODELS` | フォールバックモデル。カンマ区切り |
| `SUPPORT_AI_RUNBOOK_MODEL` | runbook生成・実行結果整理向けの強めのモデル。未設定なら `SUPPORT_AI_MODEL` を使う |
| `SUPPORT_AI_RUNBOOK_FALLBACK_MODELS` | runbook向けフォールバックモデル。カンマ区切り |
| `SUPPORT_AI_CONTEXT` | LLM に渡すサポート対象の説明 |
| `SUPPORT_AI_ENVIRONMENT_CANDIDATES` | triage AI が本文から推定してよい environment 候補。カンマ区切り |
| `SUPPORT_AI_MACHINE_CANDIDATES` | triage AI が本文から推定してよい machine 候補。カンマ区切り |
| `SUPPORT_AI_MACHINE_ALIAS_MAP` | machine の一意な表記ゆれを canonical 名へ正規化する JSON map |
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

Zendesk trigger 側で対象環境や machine を判定できる場合は、次のように payload に含めます。webhook から明示された値は triage AI の文面推定より優先され、Knowledge run の metadata に保存されます。

```json
{"ticket_id": 12345, "environment": "production", "machine": "target-host-01"}
```

Zendesk 側で送れない場合、triage AI は `SUPPORT_AI_ENVIRONMENT_CANDIDATES` / `SUPPORT_AI_MACHINE_CANDIDATES` の候補内から本文で高確信に特定できるものだけを採用します。候補が複数あり迷う場合や本文根拠が弱い場合は metadata を空にし、公開返信は保留して対象環境を確認する質問を出します。

machine は本来一意に決まるシステム名だけを扱います。表記ゆれは `SUPPORT_AI_MACHINE_ALIAS_MAP` で canonical 名へ正規化します。

```text
SUPPORT_AI_MACHINE_CANDIDATES=RIKYU,R-CCS Cloud
SUPPORT_AI_MACHINE_ALIAS_MAP={"RIKYU":["理究","RIKYU","Rikyu","rikyu","りきゅう"],"R-CCS Cloud":["R-CCSクラウド","R-CCS Cloud"]}
```

`GH200` や `GB200` のような機種名・GPU名・構成名は、複数の別システムを指し得るため machine alias として登録しません。そのような語だけが本文にある場合、triage AI は machine を空にし、`ask_user`、`operator_select`、または `runbook_identify` に倒します。

triage AI は対象特定の進め方を `target_resolution` として出します。

| Value | Meaning |
|---|---|
| `identified_from_webhook` | Zendesk webhook payload の明示値を採用した。generator が補正して付与する |
| `identified_from_text` | チケット本文から候補内の対象を高確信で特定した |
| `ask_user` | ユーザーに対象環境/machineを聞き返すのが最短で安全 |
| `operator_select` | 担当者がZendeskフォーム、タグ、運用文脈から選ぶべき |
| `runbook_identify` | Knowledgeや既存チケット、実機に触れない範囲の調査で特定できそう |
| `unknown_stop` | 対象不明のまま自動処理を止め、operator review に回すべき |

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

## Runbook Worker

`runbook-worker` service は Knowledge API の `status=requested` run を拾い、runbook向けモデルで実行前レビュー用の `runbook-plan` document を添付します。worker は実機操作、Zendesk投稿、公開返信を行いません。処理の流れは次の通りです。

```text
requested -> planning -> review_requested
revision_requested -> planning -> review_requested
```

生成される document には、Knowledge 照会観点、読み取り系確認、risk review、人間承認理由、停止条件、`findings` / `issue_on_run` / `summary` / `answer_draft` のテンプレートを含めます。実機実行やユーザー返信は、この `planned` run を人間または後続AIが確認してから進めます。

worker は LLM の出力後にも決定論的なガードを掛けます。run の `environment` または `machine` が未特定の場合は、`requires_human_approval=true`、`answer_draft_policy=hold` に補正し、公開返信案は findings 登録後に作るプレースホルダーへ戻します。`module load`、ビルド、インストール、設定変更、ジョブ投入、ユーザーデータ参照は、この plan だけでは承認済みになりません。確認済みでない module 名、MPI 実装名、configure/build option、自前ビルド推奨、管理者作業依頼は answer draft に入れない方針です。

runbook の役割は一回で完全解決することではなく、問題解決に向けて着実に前進し、再利用できる知見を蓄積することです。chief review や revision request が広すぎる調査を要求している場合、worker は今回の runbook の scope を縮小し、今回確認する範囲、未確認として残す範囲、後続調査へ送る範囲を分けます。回答案も、その時点で確認済みの根拠から安全に言える範囲に限定します。

## Runbook Review Worker

`runbook-review-worker` service は `review_requested` の runbook plan を、実機実行前に risk / technical / chief の三段階評価へ通します。

- `runbook-risk-review`: 実機操作に係る危険度だけを確認します。エージェントが範囲外操作をする余地、実行権限、ユーザーデータ参照、サービス影響、ロールバック、人間承認、停止条件が対象です。技術的な調査効率や既知問題の調べ方は扱いません。
- `runbook-technical-review`: 調査コスト、効率、具体性、再利用性、過去知見・既知問題の活用、回答根拠の作りやすさを確認します。実行権限や破壊的操作などの実機安全性は risk review に任せます。
- `runbook-chief-review`: risk / technical の査読結果と最新 runbook plan を読み、重複、矛盾、抜け漏れ、観点混在を整理します。人間と runbook worker が見る中心のレビューです。

評価タイミングは runbook plan 生成直後です。risk / technical は専門査読として document に残りますが、最終判定は chief review が行います。chief が `pass` なら run は `review_passed` になり、人間または実機側AIの実行前確認へ進めます。chief が `revise` なら `runbook-revision-request` document を添付し、run を `revision_requested` に戻します。runbook worker は chief review と revision request を文脈として新しい `runbook-plan` を作ります。

chief review は抽象的な指摘だけで終わらせません。次の項目を出し、runbook worker がそのまま次の plan に反映できる形へ整理します。

- `Final Revise Requests`: 最終的な差し戻し事項。重複を除き、優先度順にまとめる。
- `Planner Patch Instructions`: runbook のどの section に何を追加・置換するか。Knowledge Queries、Read-only Checks、Execution Steps、Findings Template、Summary Template、Answer Draft Skeleton へ展開できる粒度で書く。
- `Evidence To Collect`: 実機AIまたは後続AIが集めるべき根拠。確認対象と期待する出力形式が分かる粒度で書く。
- `Pass Conditions`: 次回レビューで pass してよい客観条件。
- `Human Decision Needed`: 人間の運用判断が本当に必要なものだけ。AI/実機確認/後続調査化で進められるものは入れない。

査読指摘を完全に満たすために調査範囲が膨らみすぎる場合、chief review は人間へ改訂を戻す前に、scope を縮小して通せる runbook にできるかを検討します。今回の runbook で扱う範囲、後続調査へ送る範囲、回答で断定しない範囲を明示すれば pass 可能にできます。人間レビューは最終手段です。

revise は無限ループさせません。`SUPPORT_AI_RUNBOOK_MAX_REVISIONS` を超える差し戻しが必要な場合、run は `operator_review` で停止します。この場合も人間が改訂文を書く前提ではなく、縮小 runbook を承認するか、後続調査を開くか、真に運用判断が必要かを確認します。`block` 判定も同じく `operator_review` で止め、人間が方針を決めるまで自動再生成しません。

## Answer Synthesis Worker

`answer-synthesis-worker` service は `operator_review` の run を読み、実機実行者または実機AIが登録した `findings` / `issue_on_run` / `summary` / 既存 `answer_draft` から、より根拠付きの `answer_draft` を再合成します。この worker は Zendesk 投稿を行いません。

実行者が登録する `answer_draft` は生の草稿です。浅い一般論、言い過ぎ、未確認事項の断定が混じる可能性があるため、operator review で人間が見る本命は、必要に応じて合成された `answer_draft_synthesized` と `answer-quality-review` です。

合成後、worker は元の Zendesk 質問・公開コメントと最新 `answer_draft` を比較し、`answer-question-evaluation` document も添付します。これは回答案が質問に答えているか、未回答論点、根拠なし断定、推奨オペレータ操作を人間レビュー向けにまとめるものです。

合成時の方針:

- `findings` にある確認済み事実を根拠にする。
- `issue_on_run` にある未確認事項や制約を踏み越えない。
- 実行していない `module load`、ビルド、ジョブ投入、ユーザーデータ参照を確認済みとして書かない。
- 環境固有の module、MPI実装、CUDA-aware対応、サポート方針、自前ビルド推奨を未確認で断定しない。
- 回答できない場合は `safe_to_send=false` とし、追加runbookの範囲を `followup_scope` に残す。

## Runbook LLM Evaluation

Triage/followup の短い判定と、runbook生成・実行結果整理は別の能力です。runbook側は長い文脈、環境固有確認、安全な実機手順、findings / issue_on_run / summary / answer_draft への変換が必要なため、`SUPPORT_AI_RUNBOOK_MODEL` で強めのモデルを別指定できます。

代表ケースで runbook 向けモデルを評価するには、コンテナ内で次を実行します。API key は Docker secrets から読み、値は表示しません。

```bash
docker compose exec -T generator python eval_runbook_llm.py
docker compose exec -T generator python eval_runbook_llm.py --models model-a,model-b
docker compose exec -T generator python eval_runbook_llm.py --case cuda_gcc_hpcx --json
docker compose exec -T generator python eval_runbook_llm.py --relaxed-json --models Kimi-K2-Thinking,K2-Think
```

評価は、構造化出力、環境固有観点、読み取り優先・承認・停止条件、Knowledgeへの受け渡しテンプレートを簡易採点します。スコアは自動採点の目安なので、採用前には `--json` の出力を人間が読み、危険な手順や根拠のない断定がないか確認してください。

現時点の strict JSON schema 経路での推奨は `SUPPORT_AI_RUNBOOK_MODEL=Qwen/Qwen3.6-35B-A3B-FP8`、fallback は `Qwen/Qwen3.6-27B-FP8,zai-org/GLM-4.7-FP8` です。Kimi 系のように strict JSON schema と相性が悪いモデルは、`--relaxed-json` で thinking を有効にし、自由形式出力から JSON object を後処理抽出して評価します。

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

リポジトリ root の `docker-compose.yml` は `webhook`、`generator`、`poster`、`followup`、`followup-responder`、`runbook-worker`、`runbook-review-worker` を非 root ユーザーで起動します。SQLite queue は app-local bind mount `apps/zendesk-support-ai/data:/data/queue`、設定は `apps/zendesk-support-ai/config:/config` を共有します。`config/agents.json` は初回作成時に `agents.example.json` 由来で作られ、起動時同期で更新されます。

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
