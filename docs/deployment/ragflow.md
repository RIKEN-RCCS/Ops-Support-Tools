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

## Scope Not Covered

This note does not cover dataset creation, model provider registration, document
ingestion, RAGFlow backup/restore, or Knowledge API integration. Treat those as
separate steps after the Web UI deployment path is stable.
