#!/usr/bin/env python3
"""Encrypted SQLite knowledge API."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

import field_crypto

DB_PATH = Path(os.environ.get("KNOWLEDGE_DB", "/data/db.sqlite"))
WRITE_TOKEN_FILE = os.environ.get("KNOWLEDGE_API_WRITE_TOKEN_FILE", "")
WRITE_TOKEN = os.environ.get("KNOWLEDGE_API_WRITE_TOKEN", "")

app = Flask(__name__)

RUN_STATUS_HELP = (
    "requested=plan生成待ち、planning=plan生成中、review_requested=risk/technical評価待ち、"
    "risk_reviewing=risk評価中、technical_reviewing=technical評価中、"
    "revision_requested=plan差し戻し、review_passed=評価通過、"
    "executing=実行claim中、execution_failed=実行失敗、"
    "operator_review=人間判断待ち、closed=終了、superseded=別runに統合済み"
)

BASE_CSS = """
body { color: #17202a; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f7f8fa; }
header { background: #17202a; color: white; padding: 14px 22px; }
header a { color: white; margin-right: 18px; text-decoration: none; }
main { margin: 22px auto; max-width: 1120px; padding: 0 18px; }
.nav-note { color: #d6dde5; display: inline-block; font-size: 13px; margin-left: 8px; }
.grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.card { background: white; border: 1px solid #e2e6ea; padding: 14px; }
.card h2, .card h3 { margin: 0 0 8px; }
.metric { font-size: 28px; font-weight: 700; line-height: 1.1; }
.quick-links a { display: inline-block; margin: 4px 8px 4px 0; }
a[title], .badge[title] { cursor: help; }
.doc-map td:first-child { font-weight: 600; white-space: nowrap; }
.doc-map code { background: #eef1f4; padding: 1px 4px; }
table { border-collapse: collapse; width: 100%; background: white; }
th, td { border-bottom: 1px solid #e2e6ea; padding: 9px 10px; text-align: left; vertical-align: top; }
th { background: #eef1f4; font-weight: 600; }
a { color: #145dbf; }
.meta { color: #66717d; font-size: 13px; }
.badge { border-radius: 999px; display: inline-block; font-size: 12px; font-weight: 600; padding: 2px 8px; background: #eef1f4; color: #32414f; }
.badge-requested { background: #fff3cd; color: #744d00; }
.badge-planning { background: #dceeff; color: #124f84; }
.badge-review_requested, .badge-risk_reviewing, .badge-technical_reviewing { background: #e6f0ff; color: #174a8b; }
.badge-planned { background: #dff3e4; color: #1f6b35; }
.badge-review_passed { background: #d4f3dc; color: #166233; }
.badge-executing { background: #dceeff; color: #124f84; }
.badge-execution_failed { background: #fde2e1; color: #8a1c13; }
.badge-revision_requested { background: #fff0d6; color: #7a4a00; }
.badge-operator_review { background: #fde2e1; color: #8a1c13; }
.badge-closed { background: #eceff3; color: #59636e; }
.badge-superseded { background: #eceff3; color: #59636e; }
.panel { background: white; border: 1px solid #e2e6ea; padding: 14px; margin: 14px 0; }
.form-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.form-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.danger { border-color: #f1b7b2; }
.focus-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.focus-card { background: #fff; border: 1px solid #d8dee6; padding: 12px; }
.focus-card h3 { margin-top: 0; }
.issue-list { margin: 6px 0 0; padding-left: 20px; }
.issue-list li { margin-bottom: 5px; }
.review-columns { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.review-point-card { background: #fff; border: 1px solid #d8dee6; padding: 12px; }
.review-point-card h3 { margin: 0 0 8px; }
.runbook-under-review pre { max-height: 760px; }
pre { background: #101820; color: #f4f7fb; overflow: auto; padding: 14px; white-space: pre-wrap; }
input, select, textarea, button { font: inherit; padding: 6px 8px; }
textarea { box-sizing: border-box; min-height: 76px; width: 100%; }
button { cursor: pointer; }
"""


LAYOUT = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <title>{{ title }}</title>
  <style>{{ css }}</style>
</head>
<body>
  <header>
    <a href="{{ link('web_index') }}" title="全体像、文書種別マップ、最近のrunbook planとrunを見る入口">Overview</a>
    <a href="{{ link('web_runs') }}" title="調査スレッドの状態を見る場所。文章本体は各runに添付されたDocumentsにあります">Runs</a>
    <a href="{{ link('web_documents') }}" title="runbook-plan、findings、summary、answer_draftなど文章本体を探す場所">Documents</a>
    <a href="{{ link('web_handoffs') }}" title="人間、実機AI、後続AIへの受け渡し依頼や補足を見る場所">Handoffs</a>
    <span class="nav-note">runbook plans, findings, drafts, and handoffs</span>
  </header>
  <main>{{ body | safe }}</main>
</body>
</html>
"""


DOCUMENT_KIND_GUIDE = """
<table class="doc-map">
  <thead><tr><th>Document type</th><th>Where to look</th><th>What it means</th></tr></thead>
  <tbody>
    <tr>
      <td><code>runbook-plan</code></td>
      <td><a href="{{ link('web_documents', q='runbook-plan') }}" title="runbook-plan文書だけをDocumentsで絞り込みます">Documents search</a> / each <a href="{{ link('web_runs', status='planned') }}" title="runbook planが生成済みの調査runを表示します">planned run</a></td>
      <td>実機確認前の計画。Knowledge照会、read-only checks、risk review、停止条件、結果テンプレート。</td>
    </tr>
    <tr>
      <td><code>runbook-decision</code></td>
      <td><a href="{{ link('web_documents', q='runbook-decision') }}" title="runbook-decision文書だけをDocumentsで絞り込みます">Documents search</a> / attached to a run</td>
      <td>既存runに文脈追加でよいか、新規調査runが必要かの判断。</td>
    </tr>
    <tr>
      <td><code>runbook-risk-review</code></td>
      <td><a href="{{ link('web_documents', q='runbook-risk-review') }}" title="実機操作前のrisk評価文書を探します">Documents search</a> / each reviewed run</td>
      <td>実機操作、権限、ユーザーデータ、サービス影響、承認、停止条件の評価。</td>
    </tr>
    <tr>
      <td><code>runbook-technical-review</code></td>
      <td><a href="{{ link('web_documents', q='runbook-technical-review') }}" title="技術評価・既知問題確認の文書を探します">Documents search</a> / each reviewed run</td>
      <td>過去知見、既知問題、環境固有確認、未確認の断定、回答準備状況の評価。</td>
    </tr>
    <tr>
      <td><code>runbook-chief-review</code></td>
      <td><a href="{{ link('web_documents', q='runbook-chief-review') }}" title="risk/technical査読を統合した主査レビューを探します">Documents search</a> / each reviewed run</td>
      <td>risk/technical査読の重複、矛盾、抜け漏れ、観点混在を整理した最終レビュー。</td>
    </tr>
    <tr>
      <td><code>runbook-revision-request</code></td>
      <td><a href="{{ link('web_documents', q='runbook-revision-request') }}" title="runbook planの差し戻し依頼を探します">Documents search</a> / revision_requested run</td>
      <td>risk/technical評価からrunbook planへ戻す差し戻し事項。回数上限あり。</td>
    </tr>
    <tr>
      <td><code>human-revision-request</code></td>
      <td><a href="{{ link('web_documents', q='human-revision-request') }}" title="人間が複数の修正指示をまとめて出した差し戻し依頼を探します">Documents search</a> / revision_requested run</td>
      <td>人間がMust Fix / Nice To Fix / pass条件をまとめて指定したrunbook修正依頼。自動revision上限とは別扱い。</td>
    </tr>
    <tr>
      <td><code>findings</code></td>
      <td><a href="{{ link('web_documents', q='findings') }}" title="実機・Knowledge・運用確認で分かった事実を探します">Documents search</a> / attached to a run</td>
      <td>実機・Knowledge・運用確認で分かった事実。推測と根拠を分けて残す。</td>
    </tr>
    <tr>
      <td><code>issue_on_run</code></td>
      <td><a href="{{ link('web_documents', q='issue_on_run') }}" title="runbook実行中の問題、未確認事項、停止理由を探します">Documents search</a> / run detail issue section</td>
      <td>runbook実行中の問題、未確認事項、停止理由。</td>
    </tr>
    <tr>
      <td><code>summary</code></td>
      <td><a href="{{ link('web_documents', q='summary') }}" title="結論、根拠、残リスク、次アクションのまとめを探します">Documents search</a> / run summary</td>
      <td>結論、根拠、残リスク、次アクションのまとめ。</td>
    </tr>
    <tr>
      <td><code>answer_draft</code></td>
      <td><a href="{{ link('web_documents', q='answer_draft') }}" title="Zendeskへ戻す社内メモ案または公開返信案を探します">Documents search</a> / Zendesk return path</td>
      <td>Zendeskへ戻す社内メモ案または公開返信案。人間レビュー前提。</td>
    </tr>
    <tr>
      <td><code>handoff-note</code></td>
      <td><a href="{{ link('web_handoffs') }}" title="人間、実機AI、後続AIへの受け渡し記録を表示します">Handoffs</a></td>
      <td>人間、実機AI、後続AIへ渡す補足、依頼、受け渡し記録。</td>
    </tr>
    <tr>
      <td><code>operator-note</code></td>
      <td><a href="{{ link('web_documents', q='operator-note') }}" title="人間がKnowledge画面で行った判断・操作記録を探します">Documents search</a> / attached to a run</td>
      <td>人間がKnowledge画面で行った対象設定、差し戻し、handoff、終了などの操作記録。</td>
    </tr>
  </tbody>
</table>
"""


def _render(title: str, body: str, **context: Any) -> str:
    doc_kind_guide = render_template_string(DOCUMENT_KIND_GUIDE, link=_web_url)
    inner = render_template_string(body, link=_web_url, doc_kind_guide=doc_kind_guide, **context)
    return render_template_string(LAYOUT, title=title, css=BASE_CSS, body=inner, link=_web_url)


def _web_prefix() -> str:
    return "/knowledge" if request.path == "/knowledge" or request.path.startswith("/knowledge/") else ""


def _web_url(endpoint: str, **values: Any) -> str:
    path = url_for(endpoint, **values)
    if path == "/knowledge" or path.startswith("/knowledge/"):
        return path
    return _web_prefix() + path


def _fmt_ts(value: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
    except (TypeError, ValueError):
        return ""


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY,
              ticket_id INTEGER,
              kind TEXT NOT NULL,
              title TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              summary_ciphertext TEXT NOT NULL DEFAULT '',
              tags_json TEXT NOT NULL DEFAULT '[]',
              source TEXT NOT NULL DEFAULT '',
              environment TEXT NOT NULL DEFAULT '',
              machine TEXT NOT NULL DEFAULT '',
              path TEXT NOT NULL,
              body_sha256 TEXT NOT NULL,
              body_ciphertext TEXT NOT NULL DEFAULT '',
              encrypted_version TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY,
              ticket_id INTEGER,
              runbook TEXT NOT NULL DEFAULT '',
              runbook_ciphertext TEXT NOT NULL DEFAULT '',
              environment TEXT NOT NULL DEFAULT '',
              machine TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'created',
              claimed_by TEXT NOT NULL DEFAULT '',
              claim_token TEXT NOT NULL DEFAULT '',
              claimed_at INTEGER NOT NULL DEFAULT 0,
              lease_until INTEGER NOT NULL DEFAULT 0,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              issue_on_run TEXT NOT NULL DEFAULT '',
              issue_on_run_ciphertext TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '',
              summary_ciphertext TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_documents (
              run_id TEXT NOT NULL,
              document_id TEXT NOT NULL,
              role TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              PRIMARY KEY (run_id, document_id),
              FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS document_handoffs (
              id TEXT PRIMARY KEY,
              document_id TEXT NOT NULL,
              ticket_id INTEGER,
              environment TEXT NOT NULL DEFAULT '',
              machine TEXT NOT NULL DEFAULT '',
              channel TEXT NOT NULL DEFAULT '',
              recipient TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'requested',
              note TEXT NOT NULL DEFAULT '',
              note_ciphertext TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            """
        )
        _ensure_column(conn, "documents", "summary_ciphertext", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "documents", "body_ciphertext", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "documents", "encrypted_version", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "documents", "environment", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "documents", "machine", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "runbook_ciphertext", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "issue_on_run_ciphertext", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "summary_ciphertext", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "environment", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "machine", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "claimed_by", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "claim_token", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "claimed_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "lease_until", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "document_handoffs", "note_ciphertext", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "document_handoffs", "environment", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "document_handoffs", "machine", "TEXT NOT NULL DEFAULT ''")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _json_error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def _write_token() -> str:
    if WRITE_TOKEN_FILE:
        return Path(WRITE_TOKEN_FILE).read_text(encoding="utf-8").strip()
    return WRITE_TOKEN.strip()


def _require_write_token() -> None:
    expected = _write_token()
    if not expected:
        return
    header = str(request.headers.get("Authorization") or "")
    prefix = "Bearer "
    if not header.startswith(prefix) or header[len(prefix):].strip() != expected:
        raise PermissionError("valid bearer token is required")


def _check_write_token() -> Any | None:
    try:
        _require_write_token()
    except PermissionError as exc:
        return _json_error(str(exc), 401)
    return None


def _decrypt_field(ciphertext: str, fallback: str = "") -> str:
    return field_crypto.decrypt_text(ciphertext) if ciphertext else fallback


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["summary"] = _decrypt_field(data.get("summary_ciphertext", ""), data.get("summary", ""))
    data["tags"] = json.loads(data.pop("tags_json") or "[]")
    data.pop("summary_ciphertext", None)
    data.pop("body_ciphertext", None)
    data.pop("encrypted_version", None)
    return data


def _run_row_to_dict(row: sqlite3.Row, *, include_claim_token: bool = False) -> dict[str, Any]:
    data = dict(row)
    data["runbook"] = _decrypt_field(data.get("runbook_ciphertext", ""), data.get("runbook", ""))
    data["issue_on_run"] = _decrypt_field(data.get("issue_on_run_ciphertext", ""), data.get("issue_on_run", ""))
    data["summary"] = _decrypt_field(data.get("summary_ciphertext", ""), data.get("summary", ""))
    data.pop("runbook_ciphertext", None)
    data.pop("issue_on_run_ciphertext", None)
    data.pop("summary_ciphertext", None)
    if not include_claim_token:
        data.pop("claim_token", None)
    return data


def _handoff_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["note"] = _decrypt_field(data.get("note_ciphertext", ""), data.get("note", ""))
    data.pop("note_ciphertext", None)
    return data


def _handoff_with_document(row: sqlite3.Row, *, include_body: bool) -> dict[str, Any]:
    data = dict(row)
    handoff = {
        "id": data["handoff_id"],
        "document_id": data["handoff_document_id"],
        "ticket_id": data["handoff_ticket_id"],
        "environment": data["handoff_environment"],
        "machine": data["handoff_machine"],
        "channel": data["channel"],
        "recipient": data["recipient"],
        "status": data["status"],
        "note": _decrypt_field(data.get("note_ciphertext", ""), data.get("note", "")),
        "created_at": data["handoff_created_at"],
        "updated_at": data["handoff_updated_at"],
    }
    doc_fields = {
        key: data[key]
        for key in (
            "id",
            "ticket_id",
            "kind",
            "title",
            "summary",
            "summary_ciphertext",
            "tags_json",
            "source",
            "environment",
            "machine",
            "path",
            "body_sha256",
            "body_ciphertext",
            "encrypted_version",
            "created_at",
            "updated_at",
        )
    }
    document = _row_to_dict(doc_fields)
    if include_body:
        document["body_md"] = _document_body(data)
    handoff["document"] = document
    return handoff


@app.get("/healthz")
def healthz():
    _init_db()
    field_crypto.load_key()
    return jsonify({"ok": True, "encrypted_fields": True})


@app.get("/")
@app.get("/knowledge/")
@app.get("/knowledge")
def web_index():
    _init_db()
    with _connect() as conn:
        counts = {
            "documents": conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"],
            "runs": conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"],
            "handoffs": conn.execute("SELECT COUNT(*) AS n FROM document_handoffs").fetchone()["n"],
            "requested_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'requested'").fetchone()["n"],
            "planning_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'planning'").fetchone()["n"],
            "planned_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'planned'").fetchone()["n"],
            "review_requested_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'review_requested'").fetchone()["n"],
            "revision_requested_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'revision_requested'").fetchone()["n"],
            "review_passed_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'review_passed'").fetchone()["n"],
            "review_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'operator_review'").fetchone()["n"],
            "requested_handoffs": conn.execute(
                "SELECT COUNT(*) AS n FROM document_handoffs WHERE status = 'requested'"
            ).fetchone()["n"],
        }
        recent_runs = conn.execute(
            """
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            GROUP BY r.id
            ORDER BY r.updated_at DESC
            LIMIT 8
            """
        ).fetchall()
        recent_plans = conn.execute(
            """
            SELECT d.*, rd.run_id, rd.role, rd.created_at AS linked_at
            FROM run_documents rd
            JOIN documents d ON d.id = rd.document_id
            WHERE d.kind = 'runbook-plan' OR rd.role = 'runbook_plan'
            ORDER BY rd.created_at DESC
            LIMIT 8
            """
        ).fetchall()
        recent_docs = conn.execute(
            """
            SELECT *
            FROM documents
            ORDER BY updated_at DESC
            LIMIT 8
            """
        ).fetchall()
    return _render(
        "Knowledge",
        """
        <h1>Knowledge Overview</h1>
        <div class="panel">
          <h2>Document Map</h2>
          <div class="meta">文章の種類ごとの入口です。runbookの状態を見るときは Runs、文章そのものを探すときは Documents を使います。</div>
          {{ doc_kind_guide | safe }}
        </div>
        <div class="grid">
          <div class="card">
            <h2>Runs</h2>
            <div class="metric">{{ counts.runs }}</div>
            <div class="quick-links">
              <a href="{{ link('web_runs', status='requested') }}" title="runbook workerまたは人間の処理待ちの調査run">requested {{ counts.requested_runs }}</a>
              <a href="{{ link('web_runs', status='planning') }}" title="runbook workerが計画生成中の調査run">planning {{ counts.planning_runs }}</a>
              <a href="{{ link('web_runs', status='review_requested') }}" title="runbook-plan文書が添付され、risk/technical評価待ちの調査run">review requested {{ counts.review_requested_runs }}</a>
              <a href="{{ link('web_runs', status='revision_requested') }}" title="risk/technical評価からplan修正へ差し戻された調査run">revision requested {{ counts.revision_requested_runs }}</a>
              <a href="{{ link('web_runs', status='review_passed') }}" title="risk/technical評価を通過し、人間の実行前確認へ進める調査run">review passed {{ counts.review_passed_runs }}</a>
              <a href="{{ link('web_runs', status='operator_review') }}" title="自動処理では進めず、人間の判断が必要な調査run">review {{ counts.review_runs }}</a>
            </div>
          </div>
          <div class="card">
            <h2>Documents</h2>
            <div class="metric">{{ counts.documents }}</div>
            <div class="quick-links">
              <a href="{{ link('web_documents', q='runbook-plan') }}" title="実機確認前の計画文書を探します">runbook plans</a>
              <a href="{{ link('web_documents', q='runbook-risk-review') }}" title="実機操作前のrisk評価文書を探します">risk reviews</a>
              <a href="{{ link('web_documents', q='runbook-technical-review') }}" title="技術評価・既知問題確認文書を探します">technical reviews</a>
              <a href="{{ link('web_documents', q='runbook-chief-review') }}" title="risk/technical査読を統合した主査レビューを探します">chief reviews</a>
              <a href="{{ link('web_documents', q='runbook-revision-request') }}" title="risk/technical評価からの差し戻し依頼を探します">revision requests</a>
              <a href="{{ link('web_documents', q='findings') }}" title="確認で分かった事実の文書を探します">findings</a>
              <a href="{{ link('web_documents', q='answer_draft') }}" title="Zendeskへ戻す返信案・社内メモ案を探します">answer drafts</a>
            </div>
          </div>
          <div class="card">
            <h2>Handoffs</h2>
            <div class="metric">{{ counts.handoffs }}</div>
            <div class="quick-links">
              <a href="{{ link('web_handoffs', status='requested') }}" title="まだ受け渡し先で処理されていない依頼">requested {{ counts.requested_handoffs }}</a>
              <a href="{{ link('web_handoffs', channel='operator-review') }}" title="人間担当者へ判断や確認を渡す依頼">operator review</a>
              <a href="{{ link('web_handoffs', channel='real-machine-agent') }}" title="実機AIまたは実機作業者へ確認を渡す依頼">real machine</a>
            </div>
          </div>
        </div>

        <h2>Latest Runbook Plans</h2>
        <table>
          <thead><tr><th>Plan</th><th>Run</th><th>Ticket</th><th>Environment</th><th>Machine</th><th>Linked</th></tr></thead>
          <tbody>
          {% for doc in recent_plans %}
            <tr>
              <td><a href="{{ link('web_document_detail', doc_id=doc.id) }}">{{ doc.title }}</a><div class="meta">{{ doc.summary }}</div></td>
              <td><a href="{{ link('web_run_detail', run_id=doc.run_id) }}">{{ doc.run_id }}</a></td>
              <td>{{ doc.ticket_id or "" }}</td>
              <td>{{ doc.environment }}</td>
              <td>{{ doc.machine }}</td>
              <td>{{ fmt(doc.linked_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>

        <h2>Latest Runs</h2>
        <table>
          <thead><tr><th>Run</th><th>Ticket</th><th>Status</th><th>Summary</th><th>Docs</th><th>Updated</th></tr></thead>
          <tbody>
          {% for run in recent_runs %}
            <tr>
              <td><a href="{{ link('web_run_detail', run_id=run.id) }}">{{ run.id }}</a></td>
              <td>{{ run.ticket_id or "" }}</td>
              <td><span class="badge badge-{{ run.status }}" title="{{ run_status_help }}">{{ run.status }}</span></td>
              <td>{{ run.summary }}</td>
              <td>{{ run.document_count }}</td>
              <td>{{ fmt(run.updated_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>

        <h2>Latest Documents</h2>
        <table>
          <thead><tr><th>Document</th><th>Kind</th><th>Ticket</th><th>Environment</th><th>Updated</th></tr></thead>
          <tbody>
          {% for doc in recent_docs %}
            <tr>
              <td><a href="{{ link('web_document_detail', doc_id=doc.id) }}">{{ doc.title }}</a><div class="meta">{{ doc.summary }}</div></td>
              <td>{{ doc.kind }}</td>
              <td>{{ doc.ticket_id or "" }}</td>
              <td>{{ doc.environment }}</td>
              <td>{{ fmt(doc.updated_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        counts=counts,
        recent_runs=[_run_row_to_dict(row) for row in recent_runs],
        recent_plans=[_row_to_dict(row) | {"run_id": row["run_id"], "role": row["role"], "linked_at": row["linked_at"]} for row in recent_plans],
        recent_docs=[_row_to_dict(row) for row in recent_docs],
        fmt=_fmt_ts,
        run_status_help=RUN_STATUS_HELP,
    )


@app.get("/documents")
@app.get("/knowledge/documents")
def web_documents():
    _init_db()
    query = str(request.args.get("q") or "").strip()
    params: list[Any] = []
    where = ""
    if query:
        where = "WHERE title LIKE ? OR kind LIKE ? OR source LIKE ? OR environment LIKE ? OR machine LIKE ? OR tags_json LIKE ?"
        params = [f"%{query}%"] * 6
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM documents
            {where}
            ORDER BY updated_at DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
    documents = [_row_to_dict(row) for row in rows]
    return _render(
        "Documents",
        """
        <h1>Documents</h1>
        <div class="panel">
          <div class="quick-links">
            <a href="{{ link('web_documents') }}" title="すべての文書を表示します">all</a>
            <a href="{{ link('web_documents', q='runbook-plan') }}" title="実機確認前の計画文書を探します">runbook plans</a>
            <a href="{{ link('web_documents', q='runbook-decision') }}" title="既存runに紐づけるか新規runにするかの判断文書を探します">runbook decisions</a>
            <a href="{{ link('web_documents', q='runbook-risk-review') }}" title="実機操作前のrisk評価文書を探します">risk reviews</a>
            <a href="{{ link('web_documents', q='runbook-technical-review') }}" title="技術評価・既知問題確認文書を探します">technical reviews</a>
            <a href="{{ link('web_documents', q='runbook-chief-review') }}" title="risk/technical査読を統合した主査レビューを探します">chief reviews</a>
            <a href="{{ link('web_documents', q='runbook-revision-request') }}" title="runbook planの差し戻し依頼を探します">revision requests</a>
            <a href="{{ link('web_documents', q='findings') }}" title="確認で分かった事実の文書を探します">findings</a>
            <a href="{{ link('web_documents', q='issue_on_run') }}" title="runbook実行中の問題、未確認事項、停止理由を探します">issues</a>
            <a href="{{ link('web_documents', q='answer_draft') }}" title="Zendeskへ戻す返信案・社内メモ案を探します">answer drafts</a>
          </div>
          <div class="meta">Documents are attached evidence, generated plans, findings, issues, summaries, drafts, and handoff notes.</div>
        </div>
        <div class="panel">
          <h2>Document Map</h2>
          {{ doc_kind_guide | safe }}
        </div>
        <form method="get" class="panel">
          <input name="q" value="{{ query }}" placeholder="title, kind, source, environment, machine, tag">
          <button type="submit">Search</button>
        </form>
        <table>
          <thead><tr><th>Title</th><th>Ticket</th><th>Environment</th><th>Machine</th><th>Kind</th><th>Tags</th><th>Updated</th></tr></thead>
          <tbody>
          {% for doc in documents %}
            <tr>
              <td><a href="{{ link('web_document_detail', doc_id=doc.id) }}">{{ doc.title }}</a><div class="meta">{{ doc.id }}</div></td>
              <td>{{ doc.ticket_id or "" }}</td>
              <td>{{ doc.environment }}</td>
              <td>{{ doc.machine }}</td>
              <td>{{ doc.kind }}</td>
              <td>{{ ", ".join(doc.tags) }}</td>
              <td>{{ fmt(doc.updated_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        documents=documents,
        query=query,
        fmt=_fmt_ts,
    )


@app.get("/documents/<doc_id>/view")
@app.get("/knowledge/documents/<doc_id>/view")
def web_document_detail(doc_id: str):
    _init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return _render("Document not found", "<h1>Document not found</h1>"), 404
    document = _row_to_dict(row)
    body_md = _document_body(row)
    return _render(
        document["title"],
        """
        <h1>{{ document.title }}</h1>
        <div class="panel">
          <div class="meta">id={{ document.id }} ticket={{ document.ticket_id or "" }} environment={{ document.environment }} machine={{ document.machine }} kind={{ document.kind }} source={{ document.source }}</div>
          <p>{{ document.summary }}</p>
          <div class="meta">tags={{ ", ".join(document.tags) }} updated={{ fmt(document.updated_at) }}</div>
        </div>
        <pre>{{ body_md }}</pre>
        """,
        document=document,
        body_md=body_md,
        fmt=_fmt_ts,
    )


@app.get("/runs")
@app.get("/knowledge/runs")
def web_runs():
    _init_db()
    status = str(request.args.get("status") or "").strip()
    ticket_id = str(request.args.get("ticket_id") or "").strip()
    environment = str(request.args.get("environment") or "").strip()
    machine = str(request.args.get("machine") or "").strip()
    filters = []
    params: dict[str, Any] = {}
    if status:
        filters.append("r.status = :status")
        params["status"] = status
    if ticket_id:
        filters.append("r.ticket_id = :ticket_id")
        params["ticket_id"] = ticket_id
    if environment:
        filters.append("r.environment = :environment")
        params["environment"] = environment
    if machine:
        filters.append("r.machine = :machine")
        params["machine"] = machine
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT r.*, COUNT(rd.document_id) AS document_count, GROUP_CONCAT(DISTINCT d.kind) AS document_kinds
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            LEFT JOIN documents d ON d.id = rd.document_id
            {where}
            GROUP BY r.id
            ORDER BY r.updated_at DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
    runs = [_run_row_to_dict(row) for row in rows]
    return _render(
        "Runs",
        """
        <h1>Runs</h1>
        <div class="panel">
          <div class="quick-links">
            <a href="{{ link('web_runs') }}" title="すべての調査runを表示します">all</a>
            <a href="{{ link('web_runs', status='requested') }}" title="runbook workerまたは人間の処理待ちの調査run">requested</a>
            <a href="{{ link('web_runs', status='planning') }}" title="runbook workerが計画生成中の調査run">planning</a>
            <a href="{{ link('web_runs', status='review_requested') }}" title="runbook-plan文書が添付され、risk/technical評価待ちの調査run">review requested</a>
            <a href="{{ link('web_runs', status='risk_reviewing') }}" title="risk評価AIが確認中の調査run">risk reviewing</a>
            <a href="{{ link('web_runs', status='technical_reviewing') }}" title="technical評価AIが確認中の調査run">technical reviewing</a>
            <a href="{{ link('web_runs', status='revision_requested') }}" title="risk/technical評価からplan修正へ差し戻された調査run">revision requested</a>
            <a href="{{ link('web_runs', status='review_passed') }}" title="risk/technical評価を通過した調査run">review passed</a>
            <a href="{{ link('web_runs', status='executing') }}" title="実機AIまたは人間がclaimして実行中の調査run">executing</a>
            <a href="{{ link('web_runs', status='execution_failed') }}" title="実行失敗として停止した調査run">execution failed</a>
            <a href="{{ link('web_runs', status='operator_review') }}" title="自動処理では進めず、人間の判断が必要な調査run">operator review</a>
          </div>
          <div class="meta">Runs are investigation threads and status trackers. The actual texts live in attached Documents: runbook-plan, runbook-risk-review, runbook-technical-review, runbook-revision-request, findings, issue_on_run, summary, and answer_draft.</div>
        </div>
        <form method="get" class="panel">
          <input name="status" value="{{ status }}" placeholder="status">
          <input name="ticket_id" value="{{ ticket_id }}" placeholder="ticket_id">
          <input name="environment" value="{{ environment }}" placeholder="environment">
          <input name="machine" value="{{ machine }}" placeholder="machine">
          <button type="submit">Filter</button>
        </form>
        <table>
          <thead><tr><th>ID</th><th>Ticket</th><th>Environment</th><th>Machine</th><th>Status</th><th>Summary</th><th>Docs</th><th>Document kinds</th><th>Updated</th></tr></thead>
          <tbody>
          {% for run in runs %}
            <tr>
              <td><a href="{{ link('web_run_detail', run_id=run.id) }}">{{ run.id }}</a></td>
              <td>{{ run.ticket_id or "" }}</td>
              <td>{{ run.environment }}</td>
              <td>{{ run.machine }}</td>
              <td><span class="badge badge-{{ run.status }}" title="{{ run_status_help }}">{{ run.status }}</span>{% if run.claimed_by %}<br><span class="meta">claimed by {{ run.claimed_by }} until {{ fmt(run.lease_until) }}</span>{% endif %}</td>
              <td>{{ run.summary }}</td>
              <td>{{ run.document_count }}</td>
              <td>{{ run.document_kinds }}</td>
              <td>{{ fmt(run.updated_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        runs=runs,
        status=status,
        ticket_id=ticket_id,
        environment=environment,
        machine=machine,
        fmt=_fmt_ts,
        run_status_help=RUN_STATUS_HELP,
    )


def _linked_sort_value(document: dict[str, Any]) -> float:
    value = document.get("linked_at") or document.get("updated_at") or document.get("created_at") or 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _latest_document(documents: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    matches = [doc for doc in documents if doc.get("kind") == kind]
    if not matches:
        return None
    return max(matches, key=_linked_sort_value)


def _markdown_sections(body_md: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in body_md.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _markdown_meta(body_md: str, key: str) -> str:
    prefix = f"- {key}:"
    for line in body_md.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _brief_items(text: str, *, limit: int = 4, width: int = 220) -> list[str]:
    items: list[str] = []
    paragraph: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if paragraph:
                items.append(" ".join(paragraph))
                paragraph = []
            continue
        if line.startswith(("- ", "* ")):
            if paragraph:
                items.append(" ".join(paragraph))
                paragraph = []
            items.append(line[2:].strip())
        elif line[:3].replace(".", "").isdigit() and ". " in line[:5]:
            if paragraph:
                items.append(" ".join(paragraph))
                paragraph = []
            items.append(line.split(". ", 1)[1].strip())
        else:
            paragraph.append(line)
    if paragraph:
        items.append(" ".join(paragraph))

    cleaned: list[str] = []
    for item in items:
        value = item.strip()
        if not value or value.lower() in {"none", "- none"}:
            continue
        if len(value) > width:
            value = value[: width - 1].rstrip() + "..."
        cleaned.append(value)
        if len(cleaned) >= limit:
            break
    return cleaned


def _review_points(document: dict[str, Any] | None, section_names: list[str], *, limit: int = 5) -> list[dict[str, str]]:
    if not document:
        return []
    body_md = str(document.get("body_md") or "")
    sections = _markdown_sections(body_md)
    points: list[dict[str, str]] = []
    for section_name in section_names:
        for item in _brief_items(sections.get(section_name, ""), limit=limit, width=260):
            points.append({"section": section_name, "text": item})
            if len(points) >= limit:
                return points
    return points


def _document_label(document: dict[str, Any] | None) -> str:
    if not document:
        return ""
    role = str(document.get("role") or "")
    kind = str(document.get("kind") or "")
    return f"{role} / {kind}" if role else kind


def _markdown_body_without_leading_meta(body_md: str) -> str:
    lines = body_md.splitlines()
    start = 0
    if lines and lines[0].startswith("#"):
        start = 1
    meta_keys = {
        "at",
        "source_run_id",
        "ticket_id",
        "environment",
        "machine",
        "runbook_document_id",
        "runbook_title",
        "answer_draft_policy",
    }
    while start < len(lines):
        line = lines[start].strip()
        if not line:
            start += 1
            continue
        if line.startswith("- ") and ":" in line:
            key = line[2:].split(":", 1)[0].strip()
            if key in meta_keys:
                start += 1
                continue
        break
    return "\n".join(lines[start:]).strip()


def _run_review_focus(documents: list[dict[str, Any]]) -> dict[str, Any]:
    latest_plan = _latest_document(documents, "runbook-plan")
    latest_risk = _latest_document(documents, "runbook-risk-review")
    latest_technical = _latest_document(documents, "runbook-technical-review")
    latest_chief = _latest_document(documents, "runbook-chief-review")
    latest_revision = _latest_document(documents, "runbook-revision-request")
    latest_human_revision = _latest_document(documents, "human-revision-request")
    chief_body = str((latest_chief or {}).get("body_md") or "")

    return {
        "latest_plan": latest_plan,
        "latest_plan_label": _document_label(latest_plan),
        "latest_plan_body": str((latest_plan or {}).get("body_md") or ""),
        "chief_document": latest_chief,
        "chief_label": _document_label(latest_chief),
        "chief_verdict": _markdown_meta(chief_body, "verdict"),
        "chief_risk_verdict": _markdown_meta(chief_body, "risk_verdict"),
        "chief_technical_verdict": _markdown_meta(chief_body, "technical_verdict"),
        "chief_final_requests": _review_points(latest_chief, ["Final Revise Requests"], limit=6),
        "chief_patch_instructions": _review_points(latest_chief, ["Planner Patch Instructions"], limit=8),
        "chief_evidence": _review_points(latest_chief, ["Evidence To Collect"], limit=8),
        "chief_risk_points": _review_points(latest_chief, ["Risk Points"], limit=4),
        "chief_technical_points": _review_points(latest_chief, ["Technical Points"], limit=4),
        "chief_conflicts": _review_points(latest_chief, ["Reviewer Conflicts"], limit=4),
        "chief_missing": _review_points(latest_chief, ["Missing Coverage"], limit=4),
        "chief_pass_conditions": _review_points(latest_chief, ["Pass Conditions"], limit=4),
        "chief_human_decisions": _review_points(latest_chief, ["Human Decision Needed"], limit=4),
        "risk_document": latest_risk,
        "risk_label": _document_label(latest_risk),
        "risk_verdict": _markdown_meta(str((latest_risk or {}).get("body_md") or ""), "verdict"),
        "risk_level": _markdown_meta(str((latest_risk or {}).get("body_md") or ""), "risk_level"),
        "risk_points": _review_points(
            latest_risk,
            ["Revise Requests", "Missing Risk Controls", "Unsafe Operations", "Missing Approvals"],
        ),
        "technical_document": latest_technical,
        "technical_label": _document_label(latest_technical),
        "technical_verdict": _markdown_meta(str((latest_technical or {}).get("body_md") or ""), "verdict"),
        "answer_readiness": _markdown_meta(str((latest_technical or {}).get("body_md") or ""), "answer_readiness"),
        "technical_points": _review_points(
            latest_technical,
            ["Revise Requests", "Missing Knowledge Queries", "Known Issue Checks", "Unsupported Assumptions"],
        ),
        "revision_document": latest_revision,
        "revision_label": _document_label(latest_revision),
        "revision_points": _review_points(
            latest_revision,
            ["Final Revise Requests", "Risk Revise Requests", "Technical Revise Requests"],
            limit=4,
        ),
        "human_revision_document": latest_human_revision,
        "human_revision_label": _document_label(latest_human_revision),
        "human_points": _review_points(
            latest_human_revision,
            ["Must Fix", "Pass If Fixed"],
            limit=4,
        ),
    }


def _run_execution_results(run: dict[str, Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
    latest_plan = _latest_document(documents, "runbook-plan")
    result_docs = [
        doc
        for doc in documents
        if str(doc.get("kind") or "") in EXECUTION_RESULT_FIELDS
           or str(doc.get("role") or "") in EXECUTION_RESULT_FIELDS
    ]
    result_docs.sort(key=lambda doc: int(doc.get("linked_at") or doc.get("created_at") or 0), reverse=True)

    cards: list[dict[str, Any]] = []
    for doc in result_docs:
        body = str(doc.get("body_md") or "")
        runbook_document_id = _markdown_meta(body, "runbook_document_id")
        runbook_title = _markdown_meta(body, "runbook_title")
        if not runbook_document_id and latest_plan:
            runbook_document_id = str(latest_plan.get("id") or "")
            runbook_title = str(latest_plan.get("title") or runbook_title)
        if not runbook_title:
            runbook_title = "Inline runbook" if not latest_plan else str(latest_plan.get("title") or "Runbook plan")
        cards.append({
            "document": doc,
            "label": _document_label(doc),
            "points": _brief_items(_markdown_body_without_leading_meta(body), limit=4, width=260),
            "runbook_document_id": runbook_document_id,
            "runbook_title": runbook_title,
            "runbook_source": "document" if runbook_document_id else "inline",
        })

    return {
        "latest_plan": latest_plan,
        "latest_plan_label": _document_label(latest_plan),
        "latest_plan_body": str((latest_plan or {}).get("body_md") or run.get("runbook") or ""),
        "cards": cards,
    }


@app.get("/runs/<run_id>/view")
@app.get("/knowledge/runs/<run_id>/view")
def web_run_detail(run_id: str):
    _init_db()
    with _connect() as conn:
        run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        doc_rows = conn.execute(
            """
            SELECT d.*, rd.role, rd.created_at AS linked_at
            FROM run_documents rd
            JOIN documents d ON d.id = rd.document_id
            WHERE rd.run_id = ?
            ORDER BY rd.created_at ASC
            """,
            (run_id,),
        ).fetchall()
    if not run_row:
        return _render("Run not found", "<h1>Run not found</h1>"), 404
    run = _run_row_to_dict(run_row)
    documents = []
    for row in doc_rows:
        doc = _row_to_dict(row)
        doc["role"] = row["role"]
        doc["linked_at"] = row["linked_at"]
        doc["body_md"] = _document_body(row)
        documents.append(doc)
    review_focus = _run_review_focus(documents)
    execution_results = _run_execution_results(run, documents)
    return _render(
        f"Run {run_id}",
        """
        <h1>Run {{ run.id }}</h1>
        <div class="panel">
          <div class="meta">ticket={{ run.ticket_id or "" }} environment={{ run.environment }} machine={{ run.machine }} status=<span class="badge badge-{{ run.status }}" title="{{ run_status_help }}">{{ run.status }}</span> updated={{ fmt(run.updated_at) }}</div>
          {% if run.claimed_by %}
          <div class="meta">claim={{ run.claimed_by }} lease_until={{ fmt(run.lease_until) }} attempt={{ run.attempt_count }}</div>
          {% endif %}
          <p>{{ run.summary }}</p>
          <div class="quick-links">
            {% for doc in documents %}
              <a href="{{ link('web_document_detail', doc_id=doc.id) }}" title="このrunに添付された {{ doc.kind }} 文書を開きます">{{ doc.role }}: {{ doc.kind }}</a>
            {% endfor %}
          </div>
        </div>
        <div class="panel">
          <h2>Review Focus</h2>
          <div class="meta">上段は主査が統合した人間レビューポイント、下段はレビュー対象の最新runbook本文です。risk/technicalの個別査読はDocumentリンクから確認できます。</div>
          {% if review_focus.chief_document %}
          <section class="review-point-card">
            <h3>Chief Review</h3>
            <div class="meta"><a href="{{ link('web_document_detail', doc_id=review_focus.chief_document.id) }}">{{ review_focus.chief_label }}</a></div>
            <p>
              {% if review_focus.chief_verdict %}<span class="badge">verdict={{ review_focus.chief_verdict }}</span>{% endif %}
              {% if review_focus.chief_risk_verdict %}<span class="badge">risk={{ review_focus.chief_risk_verdict }}</span>{% endif %}
              {% if review_focus.chief_technical_verdict %}<span class="badge">technical={{ review_focus.chief_technical_verdict }}</span>{% endif %}
            </p>
            {% if review_focus.chief_final_requests %}
              <strong>Final Revise Requests</strong>
              <ul class="issue-list">
                {% for point in review_focus.chief_final_requests %}
                  <li>{{ point.text }}</li>
                {% endfor %}
              </ul>
            {% endif %}
            {% if review_focus.chief_patch_instructions %}
              <strong>Planner Patch Instructions</strong>
              <ul class="issue-list">
                {% for point in review_focus.chief_patch_instructions %}
                  <li>{{ point.text }}</li>
                {% endfor %}
              </ul>
            {% endif %}
            {% if review_focus.chief_evidence %}
              <strong>Evidence To Collect</strong>
              <ul class="issue-list">
                {% for point in review_focus.chief_evidence %}
                  <li>{{ point.text }}</li>
                {% endfor %}
              </ul>
            {% endif %}
            <div class="review-columns">
              {% if review_focus.chief_risk_points %}
              <div>
                <strong>Risk Points</strong>
                <ul class="issue-list">
                  {% for point in review_focus.chief_risk_points %}
                    <li>{{ point.text }}</li>
                  {% endfor %}
                </ul>
              </div>
              {% endif %}
              {% if review_focus.chief_technical_points %}
              <div>
                <strong>Technical Points</strong>
                <ul class="issue-list">
                  {% for point in review_focus.chief_technical_points %}
                    <li>{{ point.text }}</li>
                  {% endfor %}
                </ul>
              </div>
              {% endif %}
            </div>
            {% if review_focus.chief_conflicts or review_focus.chief_missing %}
            <div class="review-columns">
              {% if review_focus.chief_conflicts %}
              <div>
                <strong>Reviewer Conflicts</strong>
                <ul class="issue-list">
                  {% for point in review_focus.chief_conflicts %}
                    <li>{{ point.text }}</li>
                  {% endfor %}
                </ul>
              </div>
              {% endif %}
              {% if review_focus.chief_missing %}
              <div>
                <strong>Missing Coverage</strong>
                <ul class="issue-list">
                  {% for point in review_focus.chief_missing %}
                    <li>{{ point.text }}</li>
                  {% endfor %}
                </ul>
              </div>
              {% endif %}
            </div>
            {% endif %}
            {% if review_focus.chief_pass_conditions or review_focus.chief_human_decisions %}
            <div class="review-columns">
              {% if review_focus.chief_pass_conditions %}
              <div>
                <strong>Pass Conditions</strong>
                <ul class="issue-list">
                  {% for point in review_focus.chief_pass_conditions %}
                    <li>{{ point.text }}</li>
                  {% endfor %}
                </ul>
              </div>
              {% endif %}
              {% if review_focus.chief_human_decisions %}
              <div>
                <strong>Human Decision Needed</strong>
                <ul class="issue-list">
                  {% for point in review_focus.chief_human_decisions %}
                    <li>{{ point.text }}</li>
                  {% endfor %}
                </ul>
              </div>
              {% endif %}
            </div>
            {% endif %}
          </section>
          {% else %}
          <section class="review-point-card">
            <h3>Chief Review</h3>
            <p class="meta">このrunにはまだ主査レビューがありません。risk/technicalの個別査読はDocuments一覧から確認できます。</p>
          </section>
          {% endif %}

          {% if review_focus.human_points or review_focus.revision_points %}
          <div class="review-columns">
            {% if review_focus.human_points %}
            <section class="review-point-card">
              <h3>Human Request</h3>
              {% if review_focus.human_revision_document %}
                <div class="meta"><a href="{{ link('web_document_detail', doc_id=review_focus.human_revision_document.id) }}">{{ review_focus.human_revision_label }}</a></div>
              {% endif %}
              <ul class="issue-list">
                {% for point in review_focus.human_points %}
                  <li><strong>{{ point.section }}:</strong> {{ point.text }}</li>
                {% endfor %}
              </ul>
            </section>
            {% endif %}
            {% if review_focus.revision_points %}
            <section class="review-point-card">
              <h3>Previous Revision Request</h3>
              {% if review_focus.revision_document %}
                <div class="meta"><a href="{{ link('web_document_detail', doc_id=review_focus.revision_document.id) }}">{{ review_focus.revision_label }}</a></div>
              {% endif %}
              <ul class="issue-list">
                {% for point in review_focus.revision_points %}
                  <li><strong>{{ point.section }}:</strong> {{ point.text }}</li>
                {% endfor %}
              </ul>
            </section>
            {% endif %}
          </div>
          {% endif %}

          {% if review_focus.latest_plan %}
          <section class="runbook-under-review">
            <h3>Runbook Under Review</h3>
            <div class="meta"><a href="{{ link('web_document_detail', doc_id=review_focus.latest_plan.id) }}">{{ review_focus.latest_plan_label }}</a>{% if review_focus.latest_plan.title %}<br>{{ review_focus.latest_plan.title }}{% endif %}</div>
            <pre>{{ review_focus.latest_plan_body }}</pre>
          </section>
          {% else %}
          <p class="meta">runbook planがまだ添付されていません。</p>
          {% endif %}
        </div>
        <div class="panel">
          <h2>Execution Results</h2>
          <div class="meta">どのrunbookを実行して何が分かったかを見る場所です。各カードは実行結果documentで、対象runbookと要点を一緒に表示します。</div>
          {% if execution_results.cards %}
            <div class="review-columns">
              {% for result in execution_results.cards %}
              <section class="review-point-card">
                <h3>{{ result.document.kind }}</h3>
                <div class="meta">
                  result: <a href="{{ link('web_document_detail', doc_id=result.document.id) }}">{{ result.label }}</a><br>
                  runbook:
                  {% if result.runbook_document_id %}
                    <a href="{{ link('web_document_detail', doc_id=result.runbook_document_id) }}">{{ result.runbook_title }}</a>
                  {% else %}
                    {{ result.runbook_title }} <span class="badge">inline</span>
                  {% endif %}
                  <br>source={{ result.document.source }} linked={{ fmt(result.document.linked_at) }}
                </div>
                {% if result.points %}
                <ul class="issue-list">
                  {% for item in result.points %}
                    <li>{{ item }}</li>
                  {% endfor %}
                </ul>
                {% endif %}
              </section>
              {% endfor %}
            </div>
          {% else %}
            <p class="meta">まだ実行結果は登録されていません。runbook実行後に Register Execution Result から findings / issue_on_run / summary / answer_draft を登録してください。</p>
          {% endif %}
        </div>
        {% if run.status == "operator_review" %}
        <div class="panel danger">
          <h2>Operator Actions</h2>
          <div class="meta">AIが自動処理を止めたrunです。ここで行う操作はKnowledge上の状態更新とhandoff記録だけで、実機操作やZendesk返信は行いません。</div>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="set_target_revision">
            <h3>Set Target And Request Revision</h3>
            <div class="form-grid">
              <label>Environment<br><input name="environment" value="{{ run.environment }}"></label>
              <label>Machine<br><input name="machine" value="{{ run.machine }}"></label>
            </div>
            <label>Must Fix<br><textarea name="must_fix" placeholder="一回で必ず直してほしい点を箇条書きでまとめる"></textarea></label>
            <label>Nice To Fix<br><textarea name="nice_to_fix" placeholder="可能なら直してほしい点。未対応でも必ずしも止めない"></textarea></label>
            <label>Pass If Fixed<br><textarea name="pass_if_fixed" placeholder="Must Fixが満たされた場合にpassとしてよい条件"></textarea></label>
            <label>Review Mode<br>
              <select name="review_mode">
                <option value="check_human_fixes_first">check_human_fixes_first</option>
                <option value="full_review">full_review</option>
              </select>
            </label>
            <label>Operator note<br><textarea name="note" placeholder="対象を選んだ根拠、補足、背景"></textarea></label>
            <div class="form-actions"><button type="submit">Request Revision</button></div>
          </form>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="handoff_real_machine">
            <h3>Handoff To Real-Machine Agent</h3>
            <div class="form-grid">
              <label>Recipient<br><input name="recipient" value="real-machine-agent"></label>
              <label>Environment<br><input name="environment" value="{{ run.environment }}"></label>
              <label>Machine<br><input name="machine" value="{{ run.machine }}"></label>
            </div>
            <label>Request note<br><textarea name="note" placeholder="実機AI/実機作業者に確認してほしいこと"></textarea></label>
            <div class="form-actions"><button type="submit">Create Handoff</button></div>
          </form>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="mark_review_passed">
            <h3>Mark Review Passed</h3>
            <label>Operator note<br><textarea name="note" placeholder="人間判断で実行前確認へ進める根拠"></textarea></label>
            <div class="form-actions"><button type="submit">Mark Review Passed</button></div>
          </form>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="close_run">
            <h3>Close Run</h3>
            <label>Reason<br><textarea name="note" placeholder="終了理由、別runへ統合した場合のIDなど"></textarea></label>
            <div class="form-actions"><button type="submit">Close</button></div>
          </form>
        </div>
        {% endif %}
        <h2>Runbook</h2>
        <pre>{{ run.runbook }}</pre>
        {% if run.status != "closed" %}
        <div class="panel">
          <h2>Register Execution Result</h2>
          <div class="meta">実機AI/実機作業者がrunbook実行後の成果をKnowledgeへ戻す入口です。入力された本文は種別ごとに暗号化documentとしてrunへ添付されます。</div>
          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}">
            <input type="hidden" name="action" value="register_execution_result">
            <input type="hidden" name="runbook_document_id" value="{{ execution_results.latest_plan.id if execution_results.latest_plan else '' }}">
            <input type="hidden" name="runbook_title" value="{{ execution_results.latest_plan.title if execution_results.latest_plan else 'Inline runbook' }}">
            <div class="form-grid">
              <label>Source<br><input name="source" value="knowledge-ui"></label>
              <label>Claim token<br><input name="claim_token" placeholder="claimed runの場合は必須"></label>
              <label>Next status<br>
                <select name="next_status">
                  <option value="operator_review">operator_review</option>
                  <option value="review_passed">review_passed</option>
                  <option value="no_change">no_change</option>
                  <option value="closed">closed</option>
                </select>
              </label>
              <label>Answer draft policy<br>
                <select name="answer_draft_policy">
                  <option value="hold">hold</option>
                  <option value="internal_note">internal_note</option>
                  <option value="public_reply_draft">public_reply_draft</option>
                </select>
              </label>
            </div>
            <label>Findings<br><textarea name="findings" placeholder="確認した事実、実行したread-only check、根拠、ログ要約"></textarea></label>
            <label>Issue On Run<br><textarea name="issue_on_run" placeholder="実行中に起きた問題、未確認事項、止めた理由。問題がなければ空でよい"></textarea></label>
            <label>Summary<br><textarea name="summary" placeholder="このrunで分かったこと、次に見る人への短いまとめ"></textarea></label>
            <label>Answer Draft<br><textarea name="answer_draft" placeholder="Zendeskへ戻す候補文。未確認なら保留理由を書く"></textarea></label>
            <label><input type="checkbox" name="create_zendesk_handoff" value="1"> Create zendesk-draft handoff from answer draft</label>
            <div class="form-actions"><button type="submit">Register Result</button></div>
          </form>
        </div>
        {% endif %}
        <h2>Issue On Run</h2>
        <pre>{{ run.issue_on_run }}</pre>
        <h2>Documents</h2>
        <table>
          <thead><tr><th>Role</th><th>Title</th><th>Kind</th><th>Summary</th><th>Linked</th></tr></thead>
          <tbody>
          {% for doc in documents %}
            <tr>
              <td>{{ doc.role }}</td>
              <td><a href="{{ link('web_document_detail', doc_id=doc.id) }}">{{ doc.title }}</a></td>
              <td>{{ doc.kind }}</td>
              <td>{{ doc.summary }}</td>
              <td>{{ fmt(doc.linked_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        run=run,
        documents=documents,
        review_focus=review_focus,
        execution_results=execution_results,
        fmt=_fmt_ts,
        run_status_help=RUN_STATUS_HELP,
    )


def _attach_operator_note(
    run: dict[str, Any],
    *,
    title: str,
    summary: str,
    body_md: str,
    role: str = "operator_note",
    kind: str = "operator-note",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return _attach_run_document(
        run,
        title=title,
        summary=summary,
        body_md=body_md,
        role=role,
        kind=kind,
        tags=tags or ["operator-note"],
        source="knowledge-ui",
    )


def _attach_run_document(
    run: dict[str, Any],
    *,
    title: str,
    summary: str,
    body_md: str,
    role: str,
    kind: str,
    tags: list[str] | None = None,
    source: str = "knowledge-ui",
) -> dict[str, Any]:
    payload = {
        "role": role,
        "ticket_id": run.get("ticket_id"),
        "kind": kind,
        "title": title,
        "summary": summary,
        "body_md": body_md,
        "tags": tags or [role, kind],
        "source": source,
        "environment": run.get("environment") or "",
        "machine": run.get("machine") or "",
    }
    document = _create_document_from_payload(payload)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO run_documents (run_id, document_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (run["id"], document["id"], role, _now()),
        )
        conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (_now(), run["id"]))
    return document


EXECUTION_RESULT_FIELDS = {
    "findings": {
        "heading": "Findings",
        "summary": "Runbook execution findings were registered.",
        "tags": ["run-output", "findings"],
    },
    "issue_on_run": {
        "heading": "Issue On Run",
        "summary": "Runbook execution issues or remaining blockers were registered.",
        "tags": ["run-output", "issue-on-run"],
    },
    "summary": {
        "heading": "Summary",
        "summary": "Runbook execution summary was registered.",
        "tags": ["run-output", "summary"],
    },
    "answer_draft": {
        "heading": "Answer Draft",
        "summary": "Answer draft from runbook execution was registered.",
        "tags": ["run-output", "answer-draft"],
    },
}


def _validate_claim_for_execution_result(run: dict[str, Any], payload: dict[str, Any]) -> bool:
    if run.get("status") != "executing" or not run.get("claim_token"):
        return False
    token = str(payload.get("claim_token") or "").strip()
    if not token:
        raise ValueError("claim_token is required for claimed executing runs")
    if token != str(run.get("claim_token") or ""):
        raise ValueError("claim_token mismatch")
    if int(run.get("lease_until") or 0) <= _now():
        raise ValueError("claim lease expired")
    return True


def _register_execution_result(
    run: dict[str, Any],
    payload: dict[str, Any],
    *,
    source: str = "knowledge-ui",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    values = {
        field: str(payload.get(field) or "").strip()
        for field in EXECUTION_RESULT_FIELDS
    }
    if not any(values.values()):
        raise ValueError("at least one execution result field is required")

    answer_policy = str(payload.get("answer_draft_policy") or "hold").strip() or "hold"
    if answer_policy not in {"hold", "internal_note", "public_reply_draft"}:
        answer_policy = "hold"
    next_status = str(payload.get("next_status") or "").strip()
    if next_status and next_status != "no_change" and next_status not in {"operator_review", "review_passed", "closed", "execution_failed"}:
        raise ValueError("next_status must be operator_review, review_passed, closed, execution_failed, or no_change")
    claim_validated = _validate_claim_for_execution_result(run, payload)
    runbook_document_id = str(payload.get("runbook_document_id") or "").strip()
    runbook_title = str(payload.get("runbook_title") or "").strip()

    documents: list[dict[str, Any]] = []
    now_text = _fmt_ts(_now())
    for field, value in values.items():
        if not value:
            continue
        spec = EXECUTION_RESULT_FIELDS[field]
        title = f"{spec['heading']} for run {run['id']}"
        body = (
            f"# {spec['heading']}\n\n"
            f"- at: {now_text}\n"
            f"- source_run_id: {run['id']}\n"
            f"- ticket_id: {run.get('ticket_id') or ''}\n"
            f"- environment: {run.get('environment') or ''}\n"
            f"- machine: {run.get('machine') or ''}\n"
            f"- runbook_document_id: {runbook_document_id}\n"
            f"- runbook_title: {runbook_title}\n"
        )
        if field == "answer_draft":
            body += f"- answer_draft_policy: {answer_policy}\n"
        body += f"\n{value}\n"
        documents.append(
            _attach_run_document(
                run,
                title=title,
                summary=spec["summary"],
                body_md=body,
                role=field,
                kind=field,
                tags=list(spec["tags"]),
                source=source,
            )
        )

    updates: dict[str, Any] = {}
    if values["summary"]:
        updates.update({"summary": "", "_summary_plain": values["summary"]})
    if values["issue_on_run"]:
        updates.update({"issue_on_run": "", "_issue_plain": values["issue_on_run"]})
    if next_status and next_status != "no_change":
        updates["status"] = next_status
        if claim_validated and next_status != "executing":
            updates.update({
                "claimed_by": "",
                "claim_token": "",
                "claimed_at": 0,
                "lease_until": 0,
            })
    if updates:
        _update_run_fields(run["id"], updates)

    handoff: dict[str, Any] | None = None
    create_handoff = payload.get("create_zendesk_handoff") in {True, "1", "true", "yes", "on"}
    if create_handoff and values["answer_draft"]:
        answer_doc = next((doc for doc in reversed(documents) if doc.get("kind") == "answer_draft"), None)
        if answer_doc:
            handoff_id = str(uuid.uuid4())
            note = "Answer draft registered from runbook execution; review before posting to Zendesk."
            now = _now()
            with _connect() as conn:
                conn.execute(
                    """
                    INSERT INTO document_handoffs
                      (id, document_id, ticket_id, environment, machine, channel, recipient, status, note, note_ciphertext, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        handoff_id,
                        answer_doc["id"],
                        run.get("ticket_id"),
                        run.get("environment") or "",
                        run.get("machine") or "",
                        "zendesk-draft",
                        "support-agent",
                        "requested",
                        "",
                        field_crypto.encrypt_text(note),
                        now,
                        now,
                    ),
                )
            handoff = {
                "id": handoff_id,
                "document_id": answer_doc["id"],
                "channel": "zendesk-draft",
                "recipient": "support-agent",
                "status": "requested",
            }

    return documents, handoff


def _update_run_fields(run_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    if "summary" in updates:
        updates["summary"] = ""
        updates["summary_ciphertext"] = field_crypto.encrypt_text(str(updates.pop("_summary_plain")))
    if "issue_on_run" in updates:
        updates["issue_on_run"] = ""
        updates["issue_on_run_ciphertext"] = field_crypto.encrypt_text(str(updates.pop("_issue_plain")))
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{key} = :{key}" for key in updates)
    updates["id"] = run_id
    with _connect() as conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE id = :id", updates)


@app.post("/runs/<run_id>/operator-action")
@app.post("/knowledge/runs/<run_id>/operator-action")
def web_run_operator_action(run_id: str):
    _init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return _render("Run not found", "<h1>Run not found</h1>"), 404
    run = _run_row_to_dict(row, include_claim_token=True)
    action = str(request.form.get("action") or "").strip()
    note = str(request.form.get("note") or "").strip()
    now_text = _fmt_ts(_now())

    if action == "set_target_revision":
        environment = str(request.form.get("environment") or "").strip()
        machine = str(request.form.get("machine") or "").strip()
        must_fix = str(request.form.get("must_fix") or "").strip()
        nice_to_fix = str(request.form.get("nice_to_fix") or "").strip()
        pass_if_fixed = str(request.form.get("pass_if_fixed") or "").strip()
        review_mode = str(request.form.get("review_mode") or "check_human_fixes_first").strip()
        if review_mode not in {"check_human_fixes_first", "full_review"}:
            review_mode = "check_human_fixes_first"
        summary = "Human requested runbook revision with bundled fix instructions."
        body = (
            "# Human Revision Request\n\n"
            f"- at: {now_text}\n"
            f"- environment: {environment}\n"
            f"- machine: {machine}\n"
            f"- review_mode: {review_mode}\n"
            "- next_status: revision_requested\n\n"
            "## Must Fix\n"
            f"{must_fix or '- none'}\n\n"
            "## Nice To Fix\n"
            f"{nice_to_fix or '- none'}\n\n"
            "## Pass If Fixed\n"
            f"{pass_if_fixed or 'Must Fixがすべて満たされ、新しい実機操作リスクが増えていなければpass寄りでよい。'}\n\n"
            "## Note\n"
            f"{note or 'none'}\n"
        )
        run_for_doc = dict(run, environment=environment, machine=machine)
        _attach_operator_note(
            run_for_doc,
            title=f"Human revision request for run {run_id}",
            summary=summary,
            body_md=body,
            role="human_revision_request",
            kind="human-revision-request",
            tags=["operator-note", "human-revision-request", "revision-request", review_mode],
        )
        _update_run_fields(run_id, {
            "environment": environment,
            "machine": machine,
            "status": "revision_requested",
            "summary": "",
            "_summary_plain": summary,
            "issue_on_run": "",
            "_issue_plain": "",
        })
    elif action == "handoff_real_machine":
        environment = str(request.form.get("environment") or run.get("environment") or "").strip()
        machine = str(request.form.get("machine") or run.get("machine") or "").strip()
        recipient = str(request.form.get("recipient") or "real-machine-agent").strip()
        title = f"Real-machine handoff for run {run_id}"
        summary = "Operator requested real-machine investigation."
        body = (
            "# Real-Machine Handoff\n\n"
            f"- at: {now_text}\n"
            f"- source_run_id: {run_id}\n"
            f"- ticket_id: {run.get('ticket_id') or ''}\n"
            f"- environment: {environment}\n"
            f"- machine: {machine}\n"
            f"- recipient: {recipient}\n\n"
            "## Request\n"
            f"{note or 'Review the attached runbook/reviews and report findings, issue_on_run, summary, and answer_draft.'}\n"
        )
        document = _attach_operator_note(
            dict(run, environment=environment, machine=machine),
            title=title,
            summary=summary,
            body_md=body,
            role="real_machine_handoff",
            tags=["handoff-note", "real-machine-agent", "operator-note"],
        )
        handoff_id = str(uuid.uuid4())
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO document_handoffs
                  (id, document_id, ticket_id, environment, machine, channel, recipient, status, note, note_ciphertext, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    document["id"],
                    run.get("ticket_id"),
                    environment,
                    machine,
                    "real-machine-agent",
                    recipient,
                    "requested",
                    "",
                    field_crypto.encrypt_text(note),
                    _now(),
                    _now(),
                ),
            )
        _update_run_fields(run_id, {
            "environment": environment,
            "machine": machine,
            "status": "operator_review",
            "summary": "",
            "_summary_plain": "Real-machine handoff requested; waiting for findings.",
        })
    elif action == "mark_review_passed":
        summary = "Operator marked runbook review as passed."
        body = (
            "# Operator Review Passed\n\n"
            f"- at: {now_text}\n"
            "- next_status: review_passed\n\n"
            "## Note\n"
            f"{note or 'none'}\n"
        )
        _attach_operator_note(run, title=f"Operator review passed for run {run_id}", summary=summary, body_md=body)
        _update_run_fields(run_id, {
            "status": "review_passed",
            "summary": "",
            "_summary_plain": summary,
            "issue_on_run": "",
            "_issue_plain": "",
        })
    elif action == "close_run":
        summary = "Operator closed this run."
        body = (
            "# Operator Closed Run\n\n"
            f"- at: {now_text}\n"
            "- next_status: closed\n\n"
            "## Reason\n"
            f"{note or 'none'}\n"
        )
        _attach_operator_note(run, title=f"Operator closed run {run_id}", summary=summary, body_md=body)
        _update_run_fields(run_id, {
            "status": "closed",
            "summary": "",
            "_summary_plain": summary,
            "issue_on_run": "",
            "_issue_plain": note,
        })
    elif action == "register_execution_result":
        source = str(request.form.get("source") or "knowledge-ui").strip() or "knowledge-ui"
        try:
            documents, handoff = _register_execution_result(
                run,
                {
                    "findings": request.form.get("findings"),
                    "issue_on_run": request.form.get("issue_on_run"),
                    "summary": request.form.get("summary"),
                    "answer_draft": request.form.get("answer_draft"),
                    "answer_draft_policy": request.form.get("answer_draft_policy"),
                    "runbook_document_id": request.form.get("runbook_document_id"),
                    "runbook_title": request.form.get("runbook_title"),
                    "claim_token": request.form.get("claim_token"),
                    "next_status": request.form.get("next_status"),
                    "create_zendesk_handoff": request.form.get("create_zendesk_handoff"),
                },
                source=source,
            )
        except ValueError as exc:
            return _json_error(str(exc), 400)
        registered = ", ".join(str(doc.get("kind") or doc.get("id")) for doc in documents)
        handoff_line = f"\n- zendesk_handoff_id: {handoff['id']}" if handoff else ""
        _attach_operator_note(
            run,
            title=f"Execution result registered for run {run_id}",
            summary=f"Execution result documents registered: {registered}",
            body_md=(
                "# Execution Result Registration\n\n"
                f"- at: {now_text}\n"
                f"- source: {source}\n"
                f"- documents: {registered or 'none'}"
                f"{handoff_line}\n"
            ),
            role="operator_note",
            kind="execution-result-registration",
            tags=["operator-note", "execution-result", source],
        )
    else:
        return _json_error("unknown operator action", 400)
    return redirect(_web_url("web_run_detail", run_id=run_id))


@app.get("/handoffs")
@app.get("/knowledge/handoffs")
def web_handoffs():
    _init_db()
    status = str(request.args.get("status") or "").strip()
    channel = str(request.args.get("channel") or "").strip()
    environment = str(request.args.get("environment") or "").strip()
    machine = str(request.args.get("machine") or "").strip()
    filters = []
    params: dict[str, Any] = {}
    if status:
        filters.append("h.status = :status")
        params["status"] = status
    if channel:
        filters.append("h.channel = :channel")
        params["channel"] = channel
    if environment:
        filters.append("h.environment = :environment")
        params["environment"] = environment
    if machine:
        filters.append("h.machine = :machine")
        params["machine"] = machine
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
              h.id AS handoff_id, h.document_id AS handoff_document_id, h.ticket_id AS handoff_ticket_id,
              h.environment AS handoff_environment, h.machine AS handoff_machine,
              h.channel, h.recipient, h.status, h.note, h.note_ciphertext,
              h.created_at AS handoff_created_at, h.updated_at AS handoff_updated_at,
              d.*
            FROM document_handoffs h
            JOIN documents d ON d.id = h.document_id
            {where}
            ORDER BY h.updated_at DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
    handoffs = [_handoff_with_document(row, include_body=False) for row in rows]
    return _render(
        "Handoffs",
        """
        <h1>Handoffs</h1>
        <form method="get" class="panel">
          <input name="status" value="{{ status }}" placeholder="status">
          <input name="channel" value="{{ channel }}" placeholder="channel">
          <input name="environment" value="{{ environment }}" placeholder="environment">
          <input name="machine" value="{{ machine }}" placeholder="machine">
          <button type="submit">Filter</button>
        </form>
        <table>
          <thead><tr><th>ID</th><th>Ticket</th><th>Environment</th><th>Machine</th><th>Status</th><th>Channel</th><th>Document</th><th>Updated</th></tr></thead>
          <tbody>
          {% for h in handoffs %}
            <tr>
              <td><a href="{{ link('web_handoff_detail', handoff_id=h.id) }}">{{ h.id }}</a></td>
              <td>{{ h.ticket_id or "" }}</td>
              <td>{{ h.environment }}</td>
              <td>{{ h.machine }}</td>
              <td>{{ h.status }}</td>
              <td>{{ h.channel }}</td>
              <td>{{ h.document.title }}</td>
              <td>{{ fmt(h.updated_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        handoffs=handoffs,
        status=status,
        channel=channel,
        environment=environment,
        machine=machine,
        fmt=_fmt_ts,
    )


@app.get("/handoffs/<handoff_id>/view")
@app.get("/knowledge/handoffs/<handoff_id>/view")
def web_handoff_detail(handoff_id: str):
    _init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
              h.id AS handoff_id, h.document_id AS handoff_document_id, h.ticket_id AS handoff_ticket_id,
              h.environment AS handoff_environment, h.machine AS handoff_machine,
              h.channel, h.recipient, h.status, h.note, h.note_ciphertext,
              h.created_at AS handoff_created_at, h.updated_at AS handoff_updated_at,
              d.*
            FROM document_handoffs h
            JOIN documents d ON d.id = h.document_id
            WHERE h.id = ?
            """,
            (handoff_id,),
        ).fetchone()
    if not row:
        return _render("Handoff not found", "<h1>Handoff not found</h1>"), 404
    handoff = _handoff_with_document(row, include_body=True)
    return _render(
        f"Handoff {handoff_id}",
        """
        <h1>Handoff {{ handoff.id }}</h1>
        <div class="panel">
          <div class="meta">ticket={{ handoff.ticket_id or "" }} environment={{ handoff.environment }} machine={{ handoff.machine }} status={{ handoff.status }} channel={{ handoff.channel }} recipient={{ handoff.recipient }}</div>
          <p>{{ handoff.note }}</p>
        </div>
        <h2>{{ handoff.document.title }}</h2>
        <p>{{ handoff.document.summary }}</p>
        <pre>{{ handoff.document.body_md }}</pre>
        """,
        handoff=handoff,
    )


def _create_document_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    body_md = payload.get("body_md")
    title = str(payload.get("title") or "").strip()
    kind = str(payload.get("kind") or "").strip()
    if not title:
        raise ValueError("title is required")
    if not kind:
        raise ValueError("kind is required")
    if not isinstance(body_md, str) or not body_md.strip():
        raise ValueError("body_md is required")

    tags = payload.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")

    doc_id = str(payload.get("id") or uuid.uuid4())

    created = _now()
    key = field_crypto.load_key()
    summary = str(payload.get("summary") or "")
    body_text = body_md.rstrip() + "\n"
    digest = field_crypto.hmac_text(body_text, key=key)
    record = {
        "id": doc_id,
        "ticket_id": payload.get("ticket_id"),
        "kind": kind,
        "title": title,
        "summary": "",
        "summary_ciphertext": field_crypto.encrypt_text(summary, key=key),
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "source": str(payload.get("source") or ""),
        "environment": str(payload.get("environment") or ""),
        "machine": str(payload.get("machine") or ""),
        "path": "",
        "body_sha256": digest,
        "body_ciphertext": field_crypto.encrypt_text(body_text, key=key),
        "encrypted_version": field_crypto.ENVELOPE_MARKER,
        "created_at": created,
        "updated_at": created,
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO documents
              (id, ticket_id, kind, title, summary, summary_ciphertext, tags_json, source, environment, machine, path,
               body_sha256, body_ciphertext, encrypted_version, created_at, updated_at)
            VALUES
              (:id, :ticket_id, :kind, :title, :summary, :summary_ciphertext, :tags_json, :source, :environment, :machine, :path,
               :body_sha256, :body_ciphertext, :encrypted_version, :created_at, :updated_at)
            """,
            record,
        )
    return _row_to_dict(record)


def _document_body(row: sqlite3.Row | dict[str, Any]) -> str:
    data = dict(row)
    ciphertext = data.get("body_ciphertext", "")
    if ciphertext:
        return field_crypto.decrypt_text(ciphertext)
    legacy_path = data.get("path") or ""
    if legacy_path:
        body_path = Path(legacy_path)
        return body_path.read_text(encoding="utf-8") if body_path.exists() else ""
    return ""


@app.post("/api/documents")
def create_document():
    _init_db()
    payload = request.get_json(silent=True) or {}
    try:
        document = _create_document_from_payload(payload)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except sqlite3.IntegrityError as exc:
        return _json_error(f"document conflict: {exc}", 409)
    return jsonify({"ok": True, "document": document}), 201


@app.get("/api/documents/<doc_id>")
def get_document(doc_id: str):
    _init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return _json_error("document not found", 404)
    data = _row_to_dict(row)
    data["body_md"] = _document_body(row)
    return jsonify({"ok": True, "document": data})


@app.get("/api/search")
def search_documents():
    _init_db()
    query = str(request.args.get("q") or "").strip()
    limit = min(max(int(request.args.get("limit", "20")), 1), 100)
    if not query:
        return _json_error("q is required", 400)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE title LIKE ?
               OR kind LIKE ?
               OR source LIKE ?
               OR environment LIKE ?
               OR machine LIKE ?
               OR tags_json LIKE ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            tuple([f"%{query}%"] * 6 + [limit]),
        ).fetchall()
    return jsonify({"ok": True, "documents": [_row_to_dict(row) for row in rows]})


@app.post("/api/document-handoffs")
def create_document_handoff():
    _init_db()
    payload = request.get_json(silent=True) or {}
    handoff_payload = payload.get("handoff") if isinstance(payload.get("handoff"), dict) else payload
    channel = str(handoff_payload.get("channel") or "").strip()
    recipient = str(handoff_payload.get("recipient") or "").strip()
    status = str(handoff_payload.get("status") or "requested").strip() or "requested"
    note = str(handoff_payload.get("note") or "")
    if not channel:
        return _json_error("channel is required", 400)

    try:
        document = _create_document_from_payload(payload)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except sqlite3.IntegrityError as exc:
        return _json_error(f"document conflict: {exc}", 409)

    now = _now()
    key = field_crypto.load_key()
    record = {
        "id": str(handoff_payload.get("id") or uuid.uuid4()),
        "document_id": document["id"],
        "ticket_id": payload.get("ticket_id"),
        "environment": str(handoff_payload.get("environment") or payload.get("environment") or document.get("environment") or ""),
        "machine": str(handoff_payload.get("machine") or payload.get("machine") or document.get("machine") or ""),
        "channel": channel,
        "recipient": recipient,
        "status": status,
        "note": "",
        "note_ciphertext": field_crypto.encrypt_text(note, key=key),
        "created_at": now,
        "updated_at": now,
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO document_handoffs
              (id, document_id, ticket_id, environment, machine, channel, recipient, status, note, note_ciphertext, created_at, updated_at)
            VALUES
              (:id, :document_id, :ticket_id, :environment, :machine, :channel, :recipient, :status, :note, :note_ciphertext, :created_at, :updated_at)
            """,
            record,
        )
    return jsonify({"ok": True, "handoff": _handoff_row_to_dict(record), "document": document}), 201


@app.get("/api/document-handoffs")
def list_document_handoffs():
    _init_db()
    status = str(request.args.get("status") or "").strip()
    channel = str(request.args.get("channel") or "").strip()
    recipient = str(request.args.get("recipient") or "").strip()
    ticket_id = request.args.get("ticket_id")
    environment = str(request.args.get("environment") or "").strip()
    machine = str(request.args.get("machine") or "").strip()
    include_body = str(request.args.get("include_body") or "").lower() in {"1", "true", "yes"}
    limit = min(max(int(request.args.get("limit", "50")), 1), 200)

    filters = []
    params: dict[str, Any] = {"limit": limit}
    if status:
        filters.append("h.status = :status")
        params["status"] = status
    if channel:
        filters.append("h.channel = :channel")
        params["channel"] = channel
    if recipient:
        filters.append("h.recipient = :recipient")
        params["recipient"] = recipient
    if ticket_id not in (None, ""):
        filters.append("h.ticket_id = :ticket_id")
        params["ticket_id"] = ticket_id
    if environment:
        filters.append("h.environment = :environment")
        params["environment"] = environment
    if machine:
        filters.append("h.machine = :machine")
        params["machine"] = machine
    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
              h.id AS handoff_id, h.document_id AS handoff_document_id, h.ticket_id AS handoff_ticket_id,
              h.environment AS handoff_environment, h.machine AS handoff_machine,
              h.channel, h.recipient, h.status, h.note,
              h.note_ciphertext,
              h.created_at AS handoff_created_at, h.updated_at AS handoff_updated_at,
              d.*
            FROM document_handoffs h
            JOIN documents d ON d.id = h.document_id
            {where}
            ORDER BY h.updated_at DESC
            LIMIT :limit
            """,
            params,
        ).fetchall()

    handoffs = [_handoff_with_document(row, include_body=include_body) for row in rows]
    return jsonify({"ok": True, "handoffs": handoffs})


@app.get("/api/document-handoffs/<handoff_id>")
def get_document_handoff(handoff_id: str):
    _init_db()
    include_body = str(request.args.get("include_body") or "1").lower() in {"1", "true", "yes"}
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
              h.id AS handoff_id, h.document_id AS handoff_document_id, h.ticket_id AS handoff_ticket_id,
              h.environment AS handoff_environment, h.machine AS handoff_machine,
              h.channel, h.recipient, h.status, h.note,
              h.note_ciphertext,
              h.created_at AS handoff_created_at, h.updated_at AS handoff_updated_at,
              d.*
            FROM document_handoffs h
            JOIN documents d ON d.id = h.document_id
            WHERE h.id = ?
            """,
            (handoff_id,),
        ).fetchone()
    if not row:
        return _json_error("document handoff not found", 404)
    return jsonify({"ok": True, "handoff": _handoff_with_document(row, include_body=include_body)})


@app.patch("/api/document-handoffs/<handoff_id>")
def update_document_handoff(handoff_id: str):
    _init_db()
    payload = request.get_json(silent=True) or {}
    allowed = {"status", "recipient", "environment", "machine"}
    updates = {key: str(payload[key]) for key in allowed if key in payload}
    if "note" in payload:
        updates["note"] = ""
        updates["note_ciphertext"] = field_crypto.encrypt_text(str(payload["note"]))
    if not updates:
        return _json_error("no updatable fields", 400)
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{key} = :{key}" for key in updates)
    updates["id"] = handoff_id
    with _connect() as conn:
        cur = conn.execute(f"UPDATE document_handoffs SET {assignments} WHERE id = :id", updates)
        if cur.rowcount == 0:
            return _json_error("document handoff not found", 404)
        row = conn.execute("SELECT * FROM document_handoffs WHERE id = ?", (handoff_id,)).fetchone()
    return jsonify({"ok": True, "handoff": _handoff_row_to_dict(row)})


@app.post("/api/runs")
def create_run():
    _init_db()
    payload = request.get_json(silent=True) or {}
    run_id = str(payload.get("id") or uuid.uuid4())
    now = _now()
    key = field_crypto.load_key()
    record = {
        "id": run_id,
        "ticket_id": payload.get("ticket_id"),
        "environment": str(payload.get("environment") or ""),
        "machine": str(payload.get("machine") or ""),
        "runbook": "",
        "runbook_ciphertext": field_crypto.encrypt_text(str(payload.get("runbook") or ""), key=key),
        "status": str(payload.get("status") or "created"),
        "issue_on_run": "",
        "issue_on_run_ciphertext": field_crypto.encrypt_text(str(payload.get("issue_on_run") or ""), key=key),
        "summary": "",
        "summary_ciphertext": field_crypto.encrypt_text(str(payload.get("summary") or ""), key=key),
        "created_at": now,
        "updated_at": now,
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO runs
              (id, ticket_id, environment, machine, runbook, runbook_ciphertext, status,
               issue_on_run, issue_on_run_ciphertext, summary, summary_ciphertext, created_at, updated_at)
            VALUES
              (:id, :ticket_id, :environment, :machine, :runbook, :runbook_ciphertext, :status,
               :issue_on_run, :issue_on_run_ciphertext, :summary, :summary_ciphertext, :created_at, :updated_at)
            """,
            record,
        )
    return jsonify({"ok": True, "run": _run_row_to_dict(record)}), 201


@app.get("/api/runs")
def list_runs():
    _init_db()
    status = str(request.args.get("status") or "").strip()
    ticket_id = request.args.get("ticket_id")
    environment = str(request.args.get("environment") or "").strip()
    machine = str(request.args.get("machine") or "").strip()
    limit = min(max(int(request.args.get("limit", "50")), 1), 200)

    filters = []
    params: dict[str, Any] = {"limit": limit}
    if status:
        filters.append("r.status = :status")
        params["status"] = status
    if ticket_id not in (None, ""):
        filters.append("r.ticket_id = :ticket_id")
        params["ticket_id"] = ticket_id
    if environment:
        filters.append("r.environment = :environment")
        params["environment"] = environment
    if machine:
        filters.append("r.machine = :machine")
        params["machine"] = machine
    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            {where}
            GROUP BY r.id
            ORDER BY r.updated_at DESC
            LIMIT :limit
            """,
            params,
        ).fetchall()
    return jsonify({"ok": True, "runs": [_run_row_to_dict(row) for row in rows]})


@app.get("/api/runs/<run_id>")
def get_run(run_id: str):
    _init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            WHERE r.id = ?
            GROUP BY r.id
            """,
            (run_id,),
        ).fetchone()
    if not row:
        return _json_error("run not found", 404)
    return jsonify({"ok": True, "run": _run_row_to_dict(row)})


def _lease_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = 1800
    return min(max(seconds, 60), 86400)


@app.post("/api/runs/claim")
def claim_run():
    _init_db()
    auth_error = _check_write_token()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    claimant = str(payload.get("claimant") or payload.get("claimed_by") or "").strip()
    if not claimant:
        return _json_error("claimant is required", 400)
    run_id = str(payload.get("run_id") or "").strip()
    status = str(payload.get("status") or "review_passed").strip() or "review_passed"
    ticket_id = payload.get("ticket_id")
    environment = str(payload.get("environment") or "").strip()
    machine = str(payload.get("machine") or "").strip()
    summary_contains = str(payload.get("summary_contains") or "").strip()
    document_kind = str(payload.get("document_kind") or "").strip()
    document_title_contains = str(payload.get("document_title_contains") or "").strip()
    document_source = str(payload.get("document_source") or "").strip()
    document_tag = str(payload.get("document_tag") or "").strip()
    lease_seconds = _lease_seconds(payload.get("lease_seconds"))
    now = _now()
    token = str(uuid.uuid4())

    filters = [
        """
        (
          (status = :status AND (claim_token = '' OR lease_until <= :now))
          OR (status = 'executing' AND lease_until <= :now AND :status = 'review_passed')
        )
        """
    ]
    params: dict[str, Any] = {"status": status, "now": now}
    if run_id:
        filters.append("id = :run_id")
        params["run_id"] = run_id
    if ticket_id not in (None, ""):
        filters.append("ticket_id = :ticket_id")
        params["ticket_id"] = ticket_id
    if environment:
        filters.append("environment = :environment")
        params["environment"] = environment
    if machine:
        filters.append("machine = :machine")
        params["machine"] = machine
    if summary_contains:
        filters.append("summary LIKE :summary_contains")
        params["summary_contains"] = f"%{summary_contains}%"
    doc_filters: list[str] = []
    if document_kind:
        doc_filters.append("d.kind = :document_kind")
        params["document_kind"] = document_kind
    if document_title_contains:
        doc_filters.append("d.title LIKE :document_title_contains")
        params["document_title_contains"] = f"%{document_title_contains}%"
    if document_source:
        doc_filters.append("d.source = :document_source")
        params["document_source"] = document_source
    if document_tag:
        doc_filters.append("d.tags_json LIKE :document_tag")
        params["document_tag"] = f"%{json.dumps(document_tag, ensure_ascii=False)[1:-1]}%"
    if doc_filters:
        filters.append(
            """
            EXISTS (
              SELECT 1
              FROM run_documents rd
              JOIN documents d ON d.id = rd.document_id
              WHERE rd.run_id = runs.id
                AND """
            + " AND ".join(doc_filters)
            + "\n            )"
        )
    where = " AND ".join(filters)

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""
            SELECT *
            FROM runs
            WHERE {where}
            ORDER BY updated_at ASC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            conn.rollback()
            return _json_error("no claimable run", 404)
        lease_until = now + lease_seconds
        conn.execute(
            """
            UPDATE runs
            SET status = 'executing',
                claimed_by = ?,
                claim_token = ?,
                claimed_at = ?,
                lease_until = ?,
                attempt_count = attempt_count + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (claimant, token, now, lease_until, now, row["id"]),
        )
        updated = conn.execute("SELECT * FROM runs WHERE id = ?", (row["id"],)).fetchone()
        conn.commit()
    return jsonify({"ok": True, "run": _run_row_to_dict(updated), "claim_token": token})


@app.post("/api/runs/<run_id>/claim/heartbeat")
def heartbeat_run_claim(run_id: str):
    _init_db()
    auth_error = _check_write_token()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("claim_token") or "").strip()
    if not token:
        return _json_error("claim_token is required", 400)
    now = _now()
    lease_until = now + _lease_seconds(payload.get("lease_seconds"))
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runs
            SET lease_until = ?, updated_at = ?
            WHERE id = ?
              AND claim_token = ?
              AND status = 'executing'
              AND lease_until > ?
            """,
            (lease_until, now, run_id, token, now),
        )
        if cur.rowcount == 0:
            return _json_error("claim not found, token mismatch, or lease expired", 409)
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return jsonify({"ok": True, "run": _run_row_to_dict(row)})


@app.post("/api/runs/<run_id>/claim/release")
def release_run_claim(run_id: str):
    _init_db()
    auth_error = _check_write_token()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("claim_token") or "").strip()
    if not token:
        return _json_error("claim_token is required", 400)
    next_status = str(payload.get("next_status") or "review_passed").strip() or "review_passed"
    if next_status not in {"review_passed", "operator_review", "closed", "execution_failed"}:
        return _json_error("next_status must be review_passed, operator_review, closed, or execution_failed", 400)
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE runs
            SET status = ?,
                claimed_by = '',
                claim_token = '',
                claimed_at = 0,
                lease_until = 0,
                updated_at = ?
            WHERE id = ?
              AND claim_token = ?
            """,
            (next_status, now, run_id, token),
        )
        if cur.rowcount == 0:
            return _json_error("claim not found or token mismatch", 409)
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return jsonify({"ok": True, "run": _run_row_to_dict(row)})


@app.patch("/api/runs/<run_id>")
def update_run(run_id: str):
    _init_db()
    payload = request.get_json(silent=True) or {}
    updates = {"status": str(payload["status"])} if "status" in payload else {}
    if "environment" in payload:
        updates["environment"] = str(payload["environment"])
    if "machine" in payload:
        updates["machine"] = str(payload["machine"])
    if "issue_on_run" in payload:
        updates["issue_on_run"] = ""
        updates["issue_on_run_ciphertext"] = field_crypto.encrypt_text(str(payload["issue_on_run"]))
    if "summary" in payload:
        updates["summary"] = ""
        updates["summary_ciphertext"] = field_crypto.encrypt_text(str(payload["summary"]))
    if not updates:
        return _json_error("no updatable fields", 400)
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{key} = :{key}" for key in updates)
    updates["id"] = run_id
    with _connect() as conn:
        cur = conn.execute(f"UPDATE runs SET {assignments} WHERE id = :id", updates)
        if cur.rowcount == 0:
            return _json_error("run not found", 404)
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return jsonify({"ok": True, "run": _run_row_to_dict(row)})


@app.post("/api/runs/<run_id>/documents")
def create_run_document(run_id: str):
    _init_db()
    payload = request.get_json(silent=True) or {}
    role = str(payload.get("role") or payload.get("kind") or "").strip()
    if not role:
        return _json_error("role or kind is required", 400)

    with _connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        return _json_error("run not found", 404)

    try:
        document_payload = dict(payload)
        document_payload.setdefault("environment", run["environment"])
        document_payload.setdefault("machine", run["machine"])
        document = _create_document_from_payload(document_payload)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except sqlite3.IntegrityError as exc:
        return _json_error(f"document conflict: {exc}", 409)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO run_documents (run_id, document_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, document["id"], role, _now()),
        )
        conn.execute("UPDATE runs SET updated_at = ? WHERE id = ?", (_now(), run_id))
        updated_run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    return jsonify(
        {
            "ok": True,
            "run": _run_row_to_dict(updated_run),
            "document": document,
            "link": {"run_id": run_id, "document_id": document["id"], "role": role},
        }
    ), 201


@app.post("/api/runs/<run_id>/execution-result")
def create_run_execution_result(run_id: str):
    _init_db()
    auth_error = _check_write_token()
    if auth_error:
        return auth_error
    payload = request.get_json(silent=True) or {}
    with _connect() as conn:
        run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run_row:
        return _json_error("run not found", 404)
    run = _run_row_to_dict(run_row, include_claim_token=True)
    source = str(payload.get("source") or "real-machine-agent").strip() or "real-machine-agent"
    try:
        documents, handoff = _register_execution_result(run, payload, source=source)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    with _connect() as conn:
        updated_run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return jsonify(
        {
            "ok": True,
            "run": _run_row_to_dict(updated_run),
            "documents": documents,
            "handoff": handoff,
        }
    ), 201


@app.get("/api/runs/<run_id>/documents")
def list_run_documents(run_id: str):
    _init_db()
    include_body = str(request.args.get("include_body") or "").lower() in {"1", "true", "yes"}
    with _connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return _json_error("run not found", 404)
        rows = conn.execute(
            """
            SELECT d.*, rd.role, rd.created_at AS linked_at
            FROM run_documents rd
            JOIN documents d ON d.id = rd.document_id
            WHERE rd.run_id = ?
            ORDER BY rd.created_at ASC
            """,
            (run_id,),
        ).fetchall()

    documents = []
    for row in rows:
        role = row["role"]
        linked_at = row["linked_at"]
        doc_fields = {key: row[key] for key in row.keys() if key not in {"role", "linked_at"}}
        document = _row_to_dict(doc_fields)
        document["role"] = role
        document["linked_at"] = linked_at
        if include_body:
            document["body_md"] = _document_body(row)
        documents.append(document)

    return jsonify({"ok": True, "run": _run_row_to_dict(run), "documents": documents})


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    _init_db()
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
