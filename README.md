# Ops Support Tools

HPC/AI 計算基盤の運用とユーザーサポートを支援するツール群の置き場です。

最初のアプリとして、Zendesk チケットを一次トリアージして内部メモへ書き戻す `zendesk-triage-ai` を置いています。今後、詳細調査支援、要約、ナレッジ連携などを追加する想定です。

## Apps

| App | Role |
|---|---|
| [`apps/zendesk-triage-ai`](apps/zendesk-triage-ai) | Zendesk チケットの一次トリアージ、担当候補の決定、内部メモ投稿 |

## Layout

```text
apps/
  zendesk-triage-ai/
    Dockerfile
    README.md
    *.py
docs/
docker-compose.yml
.env.example
```

## Quick Start

```bash
cp .env.example .env
docker compose up --build
```

Compose は `zendesk-triage-ai` の `webhook`、`generator`、`poster` を起動します。詳細は [`apps/zendesk-triage-ai/README.md`](apps/zendesk-triage-ai/README.md) を参照してください。
