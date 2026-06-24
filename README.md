# Ops Support Tools

HPC/AI 計算基盤の運用とユーザーサポートを支援するツール群の置き場です。

最初のアプリとして、Zendesk チケットの一次トリアージ、追加質問対応、担当者補助、ナレッジ連携を扱う `zendesk-support-ai` を置いています。

## Apps

| App | Role |
|---|---|
| [`apps/zendesk-support-ai`](apps/zendesk-support-ai) | Zendesk チケットの一次トリアージ、追加質問ドラフト、担当候補の決定、内部メモ投稿 |
| [`apps/knowledge-api`](apps/knowledge-api) | 暗号化SQLiteによる知見・runbook・調査結果・書類受け渡し API |

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
| Zendesk ticket linkage | Implemented as optional metadata | `ticket_id` は任意。Zendesk 由来の知見だけに付ける |
| Environment and machine scoping | Implemented | `environment` と `machine` で、どの環境・どの実機の知見かを分ける |
| Move / restore | Implemented | app-local な `.env`、`config/`、`data/`、`secrets/` を移せば復旧できる |

## Next Work

次に整えるべき中心は、Knowledge API を使った問題解決ループです。runbook を生成し、リスクを見て、実行結果を知見として戻し、Zendesk へ回答案を返す流れを明文化・自動化します。

| Area | Next Shape |
|---|---|
| Runbook AI generation | チケット内容、既存知見、対象 `environment` / `machine` から、実行可能な runbook を生成する |
| Runbook risk review | 実機操作、破壊的操作、権限、影響範囲、ロールバック、人間承認要否を評価する |
| Runbook execution docs | AI agent、人間、実機作業者が同じ形式で読める手順テンプレートを整備する |
| Execution result registration | 実行後に `findings`、`issue_on_run`、`summary`、`answer_draft` を Knowledge の run/document に登録する |
| Knowledge summarization | 複数の findings や runbook から再利用しやすいまとめ文書を作る |
| Zendesk return path | 回答案、社内メモ案、追加質問案を Zendesk チケットへ戻す導線を作る |
| Staleness review | 環境変更、古い手順、期限切れワークアラウンドを定期的に見直す |
| Failed-run loop | 解決しなかった場合に追加 runbook を生成し、再評価して次の実行へ回す |

当面は `knowledge-api` の `runs` と `document_handoffs` を作業受け渡し口にします。新しい worker を追加する場合も、まず `status=requested` の run/handoff を取得し、処理結果を Knowledge に戻す形に揃えます。

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
