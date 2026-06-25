# Ops Support Tools

HPC/AI 計算基盤の運用とユーザーサポートを支援するツール群の置き場です。

最初のアプリとして、Zendesk チケットの一次トリアージ、追加質問対応、担当者補助、ナレッジ連携を扱う `zendesk-support-ai` を置いています。

## Apps

| App | Role |
|---|---|
| [`apps/zendesk-support-ai`](apps/zendesk-support-ai) | Zendesk チケットの一次トリアージ、追加質問ドラフト、担当候補の決定、内部メモ投稿 |
| [`apps/knowledge-api`](apps/knowledge-api) | 暗号化SQLiteによる知見・runbook・調査結果・書類受け渡し API |
| [`apps/runbook-gateway`](apps/runbook-gateway) | 実機側Apptainer CLI gateway。許可グループの人間/AIがrunbookをclaimし、結果をKnowledgeへ戻す |

## Layout

```text
apps/
  zendesk-support-ai/
    Dockerfile
    README.md
    .env.example
    secrets/
    config/
    *.py
  knowledge-api/
    Dockerfile
    README.md
    app.py
    data/
  runbook-gateway/
    Apptainer.def
    runbook_gateway.py
docs/
docker-compose.yml
```

## Quick Start

```bash
cp apps/zendesk-support-ai/.env.example apps/zendesk-support-ai/.env
mkdir -p apps/zendesk-support-ai/secrets
docker compose up --build
```

Compose は `zendesk-support-ai` の各 worker と `knowledge-api` を起動します。詳細は [`apps/zendesk-support-ai/README.md`](apps/zendesk-support-ai/README.md) と [`apps/knowledge-api/README.md`](apps/knowledge-api/README.md) を参照してください。

## Current Coverage

現時点では、Zendesk からの入口、AI 処理の中間 queue、運用監視、知見の蓄積先までを整備しています。

| Area | Status | Notes |
|---|---|---|
| 新規チケット triage | Implemented | Zendesk webhook `/zendesk/webhook/triage` で受け、LLM 生成結果を内部メモとして戻す |
| 追加質問 followup | Implemented | Zendesk webhook `/zendesk/webhook/followup` で受け、公開エンドユーザーコメントが追加質問かを判定して返信ドラフトを内部メモ化する |
| 担当候補・対応担当フィールド | Implemented | `agents.json` と Zendesk light agent 同期、`SUPPORT_AI_ASSIGNEE_FIELD_ID` でカスタムフィールド更新 |
| Support AI queue | Implemented | SQLite queue に保存し、payload は `SUPPORT_AI_QUEUE_KEY` で暗号化する |
| Support AI monitor | Implemented | `/support-ai/monitor` で queue 状態を確認する。公開 nginx では管理IPのみ許可する |
| Knowledge database | Implemented | 暗号化 SQLite に documents、runs、document handoffs を保存する |
| Knowledge web browse | Implemented | `/knowledge/` で復号済みレコードをアプリ経由で閲覧する。公開 nginx では管理IPのみ許可する |
| Triage gate to runbook | Implemented | 環境固有知識や実機確認が必要な問い合わせは公開返信案を抑制し、Knowledge runへ送る |
| Runbook decision agent | Implemented | 同じ `ticket_id` の未完了runがある場合、本質的に追加runbookが必要かを判定し、decision documentとして残す |
| Runbook AI generation | Implemented | Knowledge run から実行前レビュー用の `runbook-plan` document を生成する |
| Runbook review agents | Implemented | risk / technical / chief review で査読し、主査が重複・矛盾・抜け漏れを整理する |
| Runbook review focus UI | Implemented | run detail で主査レビュー、具体的な改訂指示、最新runbook本文を同じ画面で確認する |
| Execution result registration | Implemented | 実行後に `findings`、`issue_on_run`、`summary`、`answer_draft` を Knowledge の run/document に登録する |
| Runbook claim / lease | Implemented | 複数の人間・AIが同じrunbookを取り合わないよう、作業開始claim、heartbeat、releaseを行う |
| Zendesk ticket linkage | Implemented as optional metadata | `ticket_id` は任意。Zendesk 由来の知見だけに付ける |
| Environment and machine scoping | Implemented | `environment` と `machine` で、どの環境・どの実機の知見かを分ける |
| Move / restore | Implemented | app-local な `.env`、`config/`、`data/`、`secrets/` を移せば復旧できる |

## Next Work

次に整えるべき中心は、Knowledge API を使った問題解決ループの後半です。runbook は一回で完全解決するためではなく、確認できる範囲を明確にし、根拠と未確認事項を残し、後続調査へつなげるために使います。主査レビューは、査読指摘を満たせない場合でも、人間へ改訂を戻す前に scope を縮小し、今回通せる runbook と後続調査に分けます。

| Area | Next Shape |
|---|---|
| Runbook execution docs | AI agent、人間、実機作業者が同じ形式で読める手順テンプレートを整備する |
| Knowledge summarization | 複数の findings や runbook から再利用しやすいまとめ文書を作る |
| Zendesk return path | 回答案、社内メモ案、追加質問案を Zendesk チケットへ戻す導線を作る |
| Staleness review | 環境変更、古い手順、期限切れワークアラウンドを定期的に見直す |
| Scoped follow-up loop | 今回のrunで扱わない範囲を後続runへ切り出し、知見を積み上げる |

当面は `knowledge-api` の `runs` と `document_handoffs` を作業受け渡し口にします。新しい worker を追加する場合も、まず `status=requested` の run/handoff を取得し、処理結果を Knowledge に戻す形に揃えます。runbook 実行後は run detail の **Register Execution Result** または `POST /api/runs/{id}/execution-result` から、`findings`、`issue_on_run`、`summary`、`answer_draft` を種別別の暗号化documentとして登録します。

実機AIまたは人間がrunbookを実行する場合の最小手順は [`apps/knowledge-api/README.md`](apps/knowledge-api/README.md) の **Runbook Execution Procedure** にまとめています。実行前に `claim_run.py` または `POST /api/runs/claim` でrunをclaimし、対象環境、対象machine、chief review、停止条件を確認し、結果はKnowledgeへ戻してからZendesk返信判断へ進めます。

## Browse

Knowledge API has a small browser UI for decrypted review through the application process:

```text
http://127.0.0.1:18180/
https://your-host.example.com/knowledge/
```

Support AI has a queue monitor for operational checks:

```text
http://127.0.0.1:18080/support-ai/monitor
https://your-host.example.com/support-ai/monitor
```

## Move / Restore

Runtime state is app-local. For a server move, stop Compose and copy the repository with these ignored runtime paths:

```text
apps/zendesk-support-ai/.env
apps/zendesk-support-ai/config/
apps/zendesk-support-ai/data/
apps/zendesk-support-ai/secrets/
apps/knowledge-api/data/
apps/knowledge-api/secrets/
```

Do not separate encrypted SQLite files from their keys.
