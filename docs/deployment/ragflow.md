# RAGFlow Deployment Notes

This note records the proof-of-concept deployment shape used to expose a
separate RAGFlow instance through the same HTTPS nginx server as the support
tools.

RAGFlow itself is not vendored into this repository. Keep the upstream RAGFlow
checkout, Docker Compose state, uploaded datasets, and RAGFlow `.env` outside
this repository.

## Current Goal

Use RAGFlow as a future RAG backend candidate without coupling it to the
Zendesk support workers yet.

The deployment target is:

```text
https://YOUR_HOST/ragflow/
```

Only management source IPs should be allowed. Do not include Zendesk webhook
source ranges in the RAGFlow UI allowlist.

## Host-Level Requirements

Elasticsearch/OpenSearch containers usually require a larger `vm.max_map_count`.
Set it on the host before starting RAGFlow:

```bash
sudo sysctl -w vm.max_map_count=262144
```

Persist this with your OS-specific sysctl configuration before production use.

## RAGFlow Compose Shape

Keep RAGFlow in a sibling or external directory, not inside this repository:

```text
/path/to/ragflow-test/
```

For a server-local proof of concept, bind RAGFlow's published web port to
localhost and proxy it through nginx:

```text
127.0.0.1:13000 -> RAGFlow web UI/API
```

Set the container timezone to the server's local timezone, for example:

```text
TZ=Asia/Tokyo
```

Do not commit RAGFlow's `.env`, generated volumes, uploaded documents, indexes,
or database files.

## Nginx Subpath Proxy

RAGFlow is not entirely subpath-native. The UI may emit absolute paths such as
`/assets/...`, `/chunk/js/...`, `/entry/js/...`, `/logo.svg`, `/login`, and API
paths. The example below keeps `/ragflow/` as the main public entry point and
adds narrow static bridges for the absolute paths seen during login testing.

Add these locations inside the existing HTTPS server block. Replace placeholders
with the local management ranges for your site.

```nginx
# RAGFlow proof-of-concept UI. Do not allow Zendesk webhook source ranges here.
location = /ragflow {
    allow MANAGEMENT_CIDR;
    allow MANAGEMENT_IP;
    deny all;

    return 301 /ragflow/;
}

location = /login {
    allow MANAGEMENT_CIDR;
    allow MANAGEMENT_IP;
    deny all;

    return 302 /ragflow/login;
}

location = /logo.svg {
    allow MANAGEMENT_CIDR;
    allow MANAGEMENT_IP;
    deny all;

    proxy_pass http://127.0.0.1:13000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
}

location ^~ /ragflow/ {
    allow MANAGEMENT_CIDR;
    allow MANAGEMENT_IP;
    deny all;

    rewrite ^/ragflow/(.*)$ /$1 break;
    proxy_pass http://127.0.0.1:13000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /ragflow;
    proxy_set_header Connection "";

    proxy_redirect / /ragflow/;
    proxy_cookie_path / /ragflow/;
    proxy_hide_header Cache-Control;
    proxy_hide_header Expires;
    add_header Cache-Control "no-store" always;
    proxy_set_header Accept-Encoding "";

    sub_filter_once off;
    sub_filter_types text/css application/javascript application/json;
    sub_filter 'href="/' 'href="/ragflow/';
    sub_filter 'src="/' 'src="/ragflow/';
    sub_filter 'url(/' 'url(/ragflow/';
    sub_filter '"/v1' '"/ragflow/v1';
    sub_filter '"/api/v1' '"/ragflow/api/v1';
    sub_filter "'/v1" "'/ragflow/v1";
    sub_filter "'/api/v1" "'/ragflow/api/v1";
    sub_filter '`/v1' '`/ragflow/v1';
    sub_filter '`/api/v1' '`/ragflow/api/v1';
    sub_filter 'basename:"/"' 'basename:"/ragflow"';
}

# RAGFlow's Vite runtime may preload static files from absolute paths.
location ^~ /assets/ {
    allow MANAGEMENT_CIDR;
    allow MANAGEMENT_IP;
    deny all;

    proxy_pass http://127.0.0.1:13000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
}

location ^~ /chunk/js/ {
    allow MANAGEMENT_CIDR;
    allow MANAGEMENT_IP;
    deny all;

    proxy_pass http://127.0.0.1:13000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
}

location ^~ /entry/js/ {
    allow MANAGEMENT_CIDR;
    allow MANAGEMENT_IP;
    deny all;

    proxy_pass http://127.0.0.1:13000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
}
```

Avoid broad JavaScript body rewrites for paths such as `"/chunk/"`. They can
corrupt React Router route definitions. Prefer narrow nginx bridges for static
files.

## Verification

After nginx reload, verify the entry page, login page, and a few static bridges:

```bash
curl -k -sS -o /tmp/ragflow-root.html -w '%{http_code} %{content_type}\n' \
  https://YOUR_HOST/ragflow/

curl -k -sS -o /tmp/ragflow-login.html -w '%{http_code} %{content_type}\n' \
  https://YOUR_HOST/ragflow/login

curl -k -sS -o /tmp/ragflow-logo.svg -w '%{http_code} %{content_type}\n' \
  https://YOUR_HOST/logo.svg

curl -k -sS -o /tmp/ragflow-static.js -w '%{http_code} %{content_type}\n' \
  https://YOUR_HOST/chunk/js/ragflow-form-603pTmlk.js
```

Expected result:

```text
200 text/html
200 text/html
200 image/svg+xml
200 application/javascript
```

Before login, `/ragflow/api/v1/users/me` and
`/ragflow/api/v1/users/me/models` may return `401`. That is normal; RAGFlow
then redirects the browser to the login screen.

## RAGFlow v0.26.3 Smoke Test

The v0.26.3 UI differs slightly from some older guides. Use this short test
before uploading real manuals or operational documents.

### 1. Add Model Providers

Open RAGFlow and go to:

```text
User icon -> Model providers
```

For an OpenAI-compatible local model server, add `OpenAI-API-Compatible`.

Add a chat model:

```text
Instance name: any local label, for example riken-llm
Model type: Chat
Model name: the exact chat model ID exposed by the local server
Base URL: http://LLM_SERVER:PORT/v1
API key: use a RAGFlow-scoped token
Max tokens: 8192, or a value suitable for the model
Tool call: off for the first test
Vision: off for the first test
```

Add an embedding model in the same provider:

```text
Model type: Embedding
Model name: the exact embedding model ID exposed by the local server
Base URL: http://LLM_SERVER:PORT/v1
API key: use the same RAGFlow-scoped token if permitted
```

Then open `System Model Settings` and set:

```text
Chat model: the added chat model
Embedding model: the added embedding model
```

Do not use `localhost` for a model server running on the host unless it is also
reachable from inside the RAGFlow container. Prefer a DNS name or IP address
that the RAGFlow container can reach.

### 2. Create a Tiny Dataset

Create a dataset for smoke testing and use the simplest parser settings:

```text
Parse type: General
Built-in: General
```

Upload a small text file with this content:

```text
# RIKYU CUDA and MPI Test Note

RIKYU is a supercomputer environment used for HPC workloads.

For CUDA and MPI programs, users should first check the available software modules before compiling. Typical commands include module avail cuda, module avail gcc, module avail mpi, module spider cuda, module spider gcc, and module spider hpcx.

If a user wants to build a CUDA-aware MPI program with GCC, the support team should confirm which CUDA, GCC, and MPI or HPC-X modules are provided on RIKYU.

This test note is only for verifying RAGFlow ingestion, embedding, retrieval, and chat. It is not an official operation policy.
```

Run parsing and wait for it to finish. The first parse can be slow because the
worker, embedding model, and indexes may all be cold. For this tiny test, a
successful parse is more important than the first-run elapsed time.

### 3. Create a Chat Assistant

RAGFlow chats are based on chat assistants, not directly on datasets.

Go to:

```text
Chat -> Create an assistant
```

Use settings like:

```text
Assistant name: RIKYU CUDA MPI smoke test
Dataset / Knowledge base: select the tiny dataset
Show quote: on
Rerank model: empty for the first test
Reasoning: off for the first test
Use knowledge graph: off for the first test
Model: the OpenAI-compatible chat model added above
```

For an HPC support-oriented prompt, replace the default system prompt with:

```text
あなたはHPCサポート向けのRAGアシスタントです。質問に対して、ナレッジベースに含まれる根拠だけを使って、簡潔で実務的な日本語で回答してください。

回答方針:
- 最終回答だけを出力してください。
- 推論過程、内部検討、自己確認、プロンプト解釈は出力しないでください。
- ナレッジベースに根拠がある内容だけを回答してください。
- 根拠が不足している場合は、推測で補わず「ナレッジベースにはお探しの回答が見つかりません！」と明記してください。
- コマンド、設定値、確認項目は箇条書きにしてください。
- サポート担当者がそのまま確認作業に使える粒度で書いてください。
- 公式回答や運用方針として断定しすぎないでください。

こちらがナレッジベースです:
{knowledge}
上記がナレッジベースです。
```

### 4. Ask a Retrieval Question

Ask:

```text
RIKYUでCUDA-aware MPIプログラムをGCCでビルドしたい場合、最初に何を確認すべきですか？
```

Expected shape:

```text
RIKYUでCUDA-aware MPIプログラムをGCCでビルドする場合、最初に利用可能なソフトウェアモジュールを確認する。

確認対象:
- CUDA
- GCC
- MPI または HPC-X

確認コマンド例:
- module avail cuda
- module avail gcc
- module avail mpi
- module spider cuda
- module spider gcc
- module spider hpcx
```

The exact citation display may vary, for example `Fig. 1` plus the source file
name. That is acceptable for this smoke test as long as the answer is grounded
in the uploaded document and the source is shown.

Passing this smoke test means the following path works:

```text
nginx -> RAGFlow UI -> model provider -> embedding -> dataset parse -> retrieval -> chat answer
```

## Scope Not Covered

This note does not cover real dataset curation, production model selection,
RAGFlow backup/restore, or Knowledge API integration. Treat those as separate
steps after the Web UI and smoke test paths are stable.
