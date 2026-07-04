#!/usr/bin/env python3
"""Encrypted SQLite knowledge API."""

from __future__ import annotations

import argparse
import hmac
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
    "routing_requested=調査要求の分解待ち、routing=調査分解中、"
    "investigation_waiting=子task/調査結果待ち、knowledge_researching=DB/Knowledge検索中、"
    "requested=plan生成待ち、planning=plan生成中、review_requested=risk/technical評価待ち、"
    "risk_reviewing=risk評価中、technical_reviewing=technical評価中、"
    "revision_requested=plan差し戻し、review_passed=評価通過、"
    "executing=実行claim中、result_registered=実行結果登録済み/回答合成待ち、"
    "answer_synthesizing=回答案合成中、answer_review=回答案レビュー待ち、"
    "task_done=子task完了/親caseで消費可能、"
    "policy_review=方針判断待ち、human_review=人間判断待ち、execution_failed=実行失敗、"
    "closed=終了、superseded=別runに統合済み"
)
TASK_TYPE_HELP = (
    "investigation_case=親調査ケース、knowledge_research=DB/Knowledge調査、"
    "real_machine_scope=実機調査scope分割待ち、real_machine=実機調査、policy_decision=人の運用方針判断、"
    "answer_synthesis=結果合成・回答案作成"
)
ALLOWED_TASK_TYPES = {
    "investigation_case",
    "knowledge_research",
    "real_machine_scope",
    "real_machine",
    "policy_decision",
    "answer_synthesis",
}
ALLOWED_RUN_STATUSES = {
    "routing_requested",
    "routing",
    "investigation_waiting",
    "knowledge_researching",
    "split_requested",
    "splitting",
    "requested",
    "planning",
    "review_requested",
    "risk_reviewing",
    "technical_reviewing",
    "planned",
    "revision_requested",
    "review_passed",
    "executing",
    "result_registered",
    "answer_synthesizing",
    "answer_review",
    "task_done",
    "policy_review",
    "human_review",
    "execution_failed",
    "closed",
    "superseded",
}

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
.badge-routing_requested, .badge-routing, .badge-planning { background: #dceeff; color: #124f84; }
.badge-investigation_waiting, .badge-knowledge_researching { background: #e8e2ff; color: #49308a; }
.badge-review_requested, .badge-risk_reviewing, .badge-technical_reviewing { background: #e6f0ff; color: #174a8b; }
.badge-planned { background: #dff3e4; color: #1f6b35; }
.badge-review_passed { background: #d4f3dc; color: #166233; }
.badge-executing { background: #dceeff; color: #124f84; }
.badge-result_registered, .badge-answer_synthesizing { background: #e6f0ff; color: #174a8b; }
.badge-answer_review { background: #fff0d6; color: #7a4a00; }
.badge-task_done { background: #d4f3dc; color: #166233; }
.badge-policy_review, .badge-human_review { background: #fde2e1; color: #8a1c13; }
.badge-execution_failed { background: #fde2e1; color: #8a1c13; }
.badge-revision_requested { background: #fff0d6; color: #7a4a00; }
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
.work-queue { border-left: 4px solid #145dbf; }
.action-box { background: #fff; border: 2px solid #145dbf; padding: 14px; margin: 14px 0; }
.action-title { font-size: 20px; font-weight: 700; margin: 0 0 6px; }
.action-list { margin: 8px 0 0; padding-left: 20px; }
.doc-review-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); margin-top: 10px; }
.doc-review-card { background: #f7f9fc; border: 1px solid #d8dee6; padding: 10px; }
.doc-review-card strong { display: block; margin-bottom: 4px; }
.priority-doc { background: #fff; border: 2px solid #d8a300; padding: 12px; margin-top: 12px; }
.priority-doc h3 { margin: 0 0 8px; }
.supporting-docs { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); margin-top: 10px; }
.supporting-doc { background: #fff; border: 1px solid #d8dee6; padding: 10px; }
.supporting-doc pre, .priority-doc pre { max-height: 360px; }
details.panel > summary { cursor: pointer; font-weight: 700; }
.answer-actions { border-color: #d8a300; }
.answer-actions h2 { margin-top: 0; }
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
    <a href="{{ link('web_index') }}" title="全体像、文書種別マップ、最近のinvestigation case/taskを見る入口">Overview</a>
    <a href="{{ link('web_runs') }}" title="investigation caseとtaskの状態を見る場所。文章本体は各runに添付されたDocumentsにあります">Runs</a>
    <a href="{{ link('web_runnable_runs') }}" title="AI worker、実機agent、人間が今処理できるrunを分類して表示します">Runnable</a>
    <a href="{{ link('web_documents') }}" title="runbook-plan、findings、summary、answer_draftなど文章本体を探す場所">Documents</a>
    <a href="{{ link('web_handoffs') }}" title="人間、実機AI、後続AIへの受け渡し依頼や補足を見る場所">Handoffs</a>
    <span class="nav-note">investigation cases, tasks, runbooks, findings, drafts, and handoffs</span>
  </header>
  <main>{{ body | safe }}</main>
</body>
</html>
"""


DOCUMENT_KIND_GUIDE = """
<div class="panel">
  <h3>Terminology</h3>
  <table class="doc-map">
    <tr><td><code>investigation_case</code></td><td>parent case run</td><td>Zendesk ticket / support question全体を束ねる調査ケース。</td></tr>
    <tr><td><code>investigation task</code></td><td>task runまたはrequest document</td><td>DB検索、実機確認、方針判断、回答合成などの小さい作業単位。</td></tr>
    <tr><td><code>runbook</code></td><td><code>real_machine</code> task</td><td>実機で実行・確認する手順。DB検索や方針判断はrunbookとは呼ばない。</td></tr>
  </table>
</div>
<table class="doc-map">
  <thead><tr><th>Document type</th><th>Where to look</th><th>What it means</th></tr></thead>
  <tbody>
    <tr>
      <td><code>runbook-plan</code></td>
      <td><a href="{{ link('web_documents', q='runbook-plan') }}" title="runbook-plan文書だけをDocumentsで絞り込みます">Documents search</a> / each <a href="{{ link('web_runs', status='planned') }}" title="runbook planが生成済みの調査runを表示します">planned run</a></td>
      <td>実機確認前の計画。Knowledge照会、read-only checks、risk review、停止条件、結果テンプレート。</td>
    </tr>
    <tr>
      <td><code>case-decision</code></td>
      <td><a href="{{ link('web_documents', q='case-decision') }}" title="case-decision文書だけをDocumentsで絞り込みます">Documents search</a> / attached to a case</td>
      <td>既存investigation caseに文脈追加でよいか、新規case/taskが必要かの判断。</td>
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
      <td><code>answer-question-evaluation</code></td>
      <td><a href="{{ link('web_documents', q='answer-question-evaluation') }}" title="回答案が元質問に答えているかの評価を探します">Documents search</a> / answer_review case</td>
      <td>最新answer_draftが元質問に答えているか、未回答論点・根拠なし断定・推奨アクションを評価する文書。</td>
    </tr>
    <tr>
      <td><code>investigation-router-plan</code></td>
      <td><a href="{{ link('web_documents', q='investigation-router-plan') }}" title="DB-firstの調査分解結果を探します">Documents search</a> / parent case</td>
      <td>DB検索を主にし、陳腐化リスクを見ながら実機調査・方針確認へ分離した計画。</td>
    </tr>
    <tr>
      <td><code>real-machine-investigation-request</code></td>
      <td><a href="{{ link('web_documents', q='real-machine-investigation-request') }}" title="実機で実行する調査要求を探します">Documents search</a> / parent case</td>
      <td>実機でないと分からない調査要求。read-onlyに限らず、capability、risk、executor modeを持つ。</td>
    </tr>
    <tr>
      <td><code>real-machine-investigation-source</code></td>
      <td><a href="{{ link('web_documents', q='real-machine-investigation-source') }}" title="実機task runの契約文書を探します">Documents search</a> / task run</td>
      <td>実機task runに添付される実行契約。required capabilities、executor mode、freshness条件を含む。</td>
    </tr>
    <tr>
      <td><code>knowledge-research-request</code></td>
      <td><a href="{{ link('web_documents', q='knowledge-research-request') }}" title="実機ではなくKnowledge/運用文書で調べる事項を探します">Documents search</a> / parent case</td>
      <td>まず確認するDB/Knowledge検索要求。いつ・どの環境・どの条件の知見か、陳腐化リスクも扱う。</td>
    </tr>
    <tr>
      <td><code>policy-decision-request</code></td>
      <td><a href="{{ link('web_documents', q='policy-decision-request') }}" title="人の運用方針判断が必要な事項を探します">Documents search</a> / parent case</td>
      <td>DBや実機では決まらない、サポート範囲・推奨可否・運用判断など人間が決める事項。</td>
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
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(int(value)))
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
              parent_run_id TEXT NOT NULL DEFAULT '',
              ticket_id INTEGER,
              task_type TEXT NOT NULL DEFAULT '',
              task_priority TEXT NOT NULL DEFAULT '',
              required_capabilities_json TEXT NOT NULL DEFAULT '[]',
              executor_mode TEXT NOT NULL DEFAULT '',
              risk_level TEXT NOT NULL DEFAULT '',
              approval_required INTEGER NOT NULL DEFAULT 0,
              runbook TEXT NOT NULL DEFAULT '',
              runbook_ciphertext TEXT NOT NULL DEFAULT '',
              environment TEXT NOT NULL DEFAULT '',
              machine TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'created',
              claimed_by TEXT NOT NULL DEFAULT '',
              claim_token TEXT NOT NULL DEFAULT '',
              claimed_at INTEGER NOT NULL DEFAULT 0,
              lease_until INTEGER NOT NULL DEFAULT 0,
              worker_claimed_by TEXT NOT NULL DEFAULT '',
              worker_claim_token TEXT NOT NULL DEFAULT '',
              worker_claimed_at INTEGER NOT NULL DEFAULT 0,
              worker_lease_until INTEGER NOT NULL DEFAULT 0,
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
        _ensure_column(conn, "runs", "parent_run_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "task_type", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "task_priority", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "required_capabilities_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "runs", "executor_mode", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "risk_level", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "approval_required", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "environment", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "machine", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "claimed_by", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "claim_token", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "claimed_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "lease_until", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "worker_claimed_by", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "worker_claim_token", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "runs", "worker_claimed_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "runs", "worker_lease_until", "INTEGER NOT NULL DEFAULT 0")
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
    if not header.startswith(prefix) or not hmac.compare_digest(header[len(prefix):].strip(), expected):
        raise PermissionError("valid bearer token is required")


def _check_write_token() -> Any | None:
    try:
        _require_write_token()
    except PermissionError as exc:
        return _json_error(str(exc), 401)
    except OSError:
        app.logger.exception("failed to read Knowledge API write token")
        return _json_error("write token is not available", 503)
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
    try:
        data["required_capabilities"] = json.loads(data.pop("required_capabilities_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        data["required_capabilities"] = []
    data["approval_required"] = bool(data.get("approval_required"))
    data.pop("runbook_ciphertext", None)
    data.pop("issue_on_run_ciphertext", None)
    data.pop("summary_ciphertext", None)
    if not include_claim_token:
        data.pop("claim_token", None)
        data.pop("worker_claim_token", None)
    return data


AI_WORKER_RUNNABLES = {
    "investigation-router-worker": {
        "statuses": ["routing_requested"],
        "claim_status": "routing",
        "task_types": ["investigation_case"],
        "description": "Split an investigation case into DB/Knowledge, real-machine, and policy tasks.",
    },
    "knowledge-research-worker": {
        "statuses": ["investigation_waiting"],
        "claim_status": "knowledge_researching",
        "task_types": ["knowledge_research"],
        "description": "Search Knowledge/DB/RAG/web sources before requesting fresh machine work.",
    },
    "real-machine-task-splitter-worker": {
        "statuses": ["split_requested"],
        "claim_status": "splitting",
        "task_types": ["real_machine_scope"],
        "description": "Split a broad real-machine scope into small independently executable tasks.",
    },
    "runbook-worker": {
        "statuses": ["requested", "revision_requested"],
        "claim_status": "planning",
        "task_types": ["real_machine"],
        "description": "Generate or revise a runbook-plan for a real-machine task.",
    },
    "runbook-review-worker": {
        "statuses": ["review_requested", "planned"],
        "claim_status": "risk_reviewing",
        "task_types": ["real_machine"],
        "description": "Run risk, technical, and chief review for a runbook-plan.",
    },
    "answer-synthesis-worker": {
        "statuses": ["result_registered"],
        "claim_status": "answer_synthesizing",
        "task_types": ["real_machine"],
        "description": "Synthesize execution results into an answer draft and evaluate coverage.",
    },
}

AI_WORKER_IN_PROGRESS = {
    "routing",
    "knowledge_researching",
    "splitting",
    "planning",
    "risk_reviewing",
    "technical_reviewing",
    "answer_synthesizing",
}

HUMAN_REQUIRED_STATUSES = {"answer_review", "policy_review", "human_review"}
TASK_DONE_STATUS = "task_done"
TASK_COMPLETE_STATUSES = {TASK_DONE_STATUS, "closed", "done"}


def _worker_spec_matches(run: dict[str, Any], spec: dict[str, Any]) -> bool:
    status = str(run.get("status") or "")
    if status not in spec["statuses"]:
        return False
    task_types = spec.get("task_types") or []
    if task_types and str(run.get("task_type") or "") not in task_types:
        return False
    return True


def _runnable_bucket(run: dict[str, Any]) -> str:
    status = str(run.get("status") or "")
    task_type = str(run.get("task_type") or "")
    if status == TASK_DONE_STATUS or (task_type == "knowledge_research" and status == "answer_review"):
        return "completed_task"
    if status in AI_WORKER_IN_PROGRESS:
        return "in_progress_ai_worker"
    if status == "executing":
        return "in_progress_real_machine"
    if status in HUMAN_REQUIRED_STATUSES:
        return "human_required"
    if status == "review_passed":
        return "real_machine_claimable"
    for spec in AI_WORKER_RUNNABLES.values():
        if _worker_spec_matches(run, spec):
            return "ai_worker_claimable"
    if status in {"execution_failed"}:
        return "failed_or_blocked"
    return "not_runnable"


def _runnable_worker_targets(run: dict[str, Any]) -> list[str]:
    return [
        name
        for name, spec in AI_WORKER_RUNNABLES.items()
        if _worker_spec_matches(run, spec)
    ]


def _child_runs_for_dependency(run_id: str) -> list[dict[str, Any]]:
    if not run_id:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            WHERE r.parent_run_id = ?
            GROUP BY r.id
            ORDER BY r.updated_at ASC
            """,
            (run_id,),
        ).fetchall()
    return [_run_row_to_dict(row) for row in rows]


def _task_done_for_parent(child: dict[str, Any]) -> bool:
    task_type = str(child.get("task_type") or "")
    status = str(child.get("status") or "")
    if task_type == "knowledge_research":
        return status in TASK_COMPLETE_STATUSES or status == "answer_review"
    if task_type == "real_machine":
        return status in {"result_registered", "answer_review"} | TASK_COMPLETE_STATUSES
    if task_type == "policy_decision":
        return status in {"answer_review"} | TASK_COMPLETE_STATUSES
    return status in {"answer_review"} | TASK_COMPLETE_STATUSES


def _run_dependency_context(run: dict[str, Any]) -> dict[str, Any]:
    status = str(run.get("status") or "")
    task_type = str(run.get("task_type") or "")
    blocked_by: list[str] = []
    unblocks_when: list[str] = []
    runnable_by: list[str] = []
    children_summary: list[dict[str, str]] = []

    worker_targets = _runnable_worker_targets(run)
    if worker_targets:
        runnable_by.extend(worker_targets)
        unblocks_when.append(f"{', '.join(worker_targets)} claims this run")
    if status == "review_passed":
        runnable_by.append("real-machine gateway/operator")
        unblocks_when.append("real-machine execution registers result_registered")
    if status in HUMAN_REQUIRED_STATUSES and not (task_type == "knowledge_research" and status == "answer_review"):
        runnable_by.append("human operator")

    if status in AI_WORKER_IN_PROGRESS and run.get("worker_claimed_by"):
        blocked_by.append(
            f"worker claim: {run.get('worker_claimed_by')} until {_fmt_ts(run.get('worker_lease_until'))}"
        )
        unblocks_when.append("worker completes, fails, or lease expires")
    if status == "executing" and run.get("claimed_by"):
        blocked_by.append(
            f"execution claim: {run.get('claimed_by')} until {_fmt_ts(run.get('lease_until'))}"
        )
        unblocks_when.append("executor registers result or releases the claim")

    if task_type == "investigation_case" or not run.get("parent_run_id"):
        children = _child_runs_for_dependency(str(run.get("id") or ""))
        pending_children = [child for child in children if not _task_done_for_parent(child)]
        for child in children:
            children_summary.append({
                "id": str(child.get("id") or ""),
                "task_type": str(child.get("task_type") or ""),
                "status": str(child.get("status") or ""),
                "done": "yes" if _task_done_for_parent(child) else "no",
            })
        if status in {"investigation_waiting", "policy_review", "answer_review", "human_review"}:
            for child in pending_children[:8]:
                child_type = str(child.get("task_type") or "task")
                child_status = str(child.get("status") or "")
                if child_type == "real_machine" and child_status == "review_passed":
                    blocked_by.append(f"{child_type} {child.get('id')}: waiting for real-machine execution")
                else:
                    blocked_by.append(f"{child_type} {child.get('id')}: {child_status}")
            if pending_children:
                unblocks_when.append(
                    "pending child tasks finish research, pass execution, register results, or close"
                )
        if status == "policy_review":
            blocked_by.append("human policy decision is required")
            unblocks_when.append("operator records policy decision or opens follow-up tasks")
        if status == "answer_review":
            blocked_by.append("human answer review is required")
            unblocks_when.append("operator approves answer, requests more investigation, or closes the case")

    if task_type == "knowledge_research" and (status in TASK_COMPLETE_STATUSES or status == "answer_review"):
        unblocks_when.append("parent case consumes this knowledge-research-result")
    elif task_type == "policy_decision" and status == "policy_review":
        blocked_by.append("policy decision cannot be decided by DB or machine checks")
        unblocks_when.append("human records the policy decision")
    elif task_type == "real_machine" and status == "review_passed":
        unblocks_when.append("a capable executor claims and runs the attached runbook-plan")

    if not blocked_by and not unblocks_when:
        if _runnable_bucket(run) == "not_runnable":
            unblocks_when.append("no automatic next step is defined for this status")
        else:
            unblocks_when.append("ready for the listed runnable target")

    return {
        "blocked_by": blocked_by,
        "unblocks_when": unblocks_when,
        "runnable_by": runnable_by,
        "children": children_summary,
    }


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


def _load_case_dashboard(*, limit: int = 24) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            WHERE r.task_type = 'investigation_case'
              AND r.status NOT IN ('closed', 'superseded')
            GROUP BY r.id
            ORDER BY r.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    cases: list[dict[str, Any]] = []
    for row in rows:
        case = _run_row_to_dict(row)
        children = _child_runs_for_dependency(str(case.get("id") or ""))
        child_counts: dict[str, int] = {}
        pending = 0
        for child in children:
            task_type = str(child.get("task_type") or "task")
            status = str(child.get("status") or "")
            key = f"{task_type}:{status}"
            child_counts[key] = child_counts.get(key, 0) + 1
            if not _task_done_for_parent(child):
                pending += 1
        case["document_count"] = row["document_count"]
        case["action"] = _run_list_action(case)
        case["dependency"] = _run_dependency_context(case)
        case["child_count"] = len(children)
        case["pending_child_count"] = pending
        case["child_counts"] = [
            {"label": label, "count": count}
            for label, count in sorted(child_counts.items())
        ]
        cases.append(case)
    return cases


@app.get("/")
@app.get("/knowledge/")
@app.get("/knowledge")
def web_index():
    _init_db()
    runnable_data = _load_runnable_runs(limit=80)
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
            "routing_requested_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'routing_requested'").fetchone()["n"],
            "investigation_waiting_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'investigation_waiting'").fetchone()["n"],
            "knowledge_researching_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'knowledge_researching'").fetchone()["n"],
            "task_done_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'task_done'").fetchone()["n"],
            "result_registered_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'result_registered'").fetchone()["n"],
            "answer_review_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'answer_review'").fetchone()["n"],
            "policy_review_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'policy_review'").fetchone()["n"],
            "human_review_runs": conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'human_review'").fetchone()["n"],
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
        work_runs = conn.execute(
            """
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            WHERE r.status IN (
              'routing_requested', 'investigation_waiting', 'knowledge_researching', 'task_done', 'result_registered', 'answer_review',
              'policy_review', 'human_review', 'review_passed',
              'revision_requested', 'execution_failed'
            )
            GROUP BY r.id
            ORDER BY
              CASE r.status
                WHEN 'answer_review' THEN 1
                WHEN 'policy_review' THEN 2
                WHEN 'human_review' THEN 3
                WHEN 'execution_failed' THEN 4
                WHEN 'result_registered' THEN 5
                WHEN 'routing_requested' THEN 6
                WHEN 'knowledge_researching' THEN 7
                WHEN 'investigation_waiting' THEN 8
                WHEN 'task_done' THEN 9
                WHEN 'revision_requested' THEN 10
                WHEN 'review_passed' THEN 11
                ELSE 9
              END,
              r.updated_at DESC
            LIMIT 12
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
        <div class="grid">
          <div class="card">
            <h2>AI Claimable</h2>
            <div class="metric">{{ runnable_counts.ai_worker_claimable }}</div>
            <div class="meta">worker registryで許可されたAI workerが今claimできるtask</div>
          </div>
          <div class="card">
            <h2>Real-Machine</h2>
            <div class="metric">{{ runnable_counts.real_machine_claimable }}</div>
            <div class="meta">実機gatewayまたは人間がclaimできるreview_passed task</div>
          </div>
          <div class="card">
            <h2>Human Required</h2>
            <div class="metric">{{ runnable_counts.human_required }}</div>
            <div class="meta">answer/policy/human reviewで人間判断が必要なrun</div>
          </div>
          <div class="card">
            <h2>In Progress</h2>
            <div class="metric">{{ runnable_counts.in_progress_ai_worker + runnable_counts.in_progress_real_machine }}</div>
            <div class="meta">AI workerまたは実機executorがlease保持中</div>
          </div>
        </div>

        <div class="panel work-queue">
          <h2>Active Investigation Cases</h2>
          <div class="meta">親case単位のダッシュボードです。子taskがどこで止まっているか、次に誰が動くかをここで見ます。</div>
          <table>
            <thead><tr><th>Case</th><th>Ticket</th><th>Status</th><th>Next action</th><th>Children</th><th>Blocked By</th><th>Updated</th></tr></thead>
            <tbody>
            {% for case in cases %}
              <tr>
                <td><a href="{{ link('web_run_detail', run_id=case.id) }}">{{ case.id }}</a><div class="meta">{{ case.environment }} / {{ case.machine }}</div></td>
                <td>{{ case.ticket_id or "" }}</td>
                <td><span class="badge badge-{{ case.status }}" title="{{ run_status_help }}">{{ case.status }}</span></td>
                <td><strong>{{ case.action.queue }}</strong><br><span class="meta">{{ case.action.next_action }}</span></td>
                <td>
                  <div class="meta">{{ case.child_count }} child task(s), {{ case.pending_child_count }} pending</div>
                  {% for item in case.child_counts[:5] %}
                    <div class="meta">{{ item.label }} = {{ item.count }}</div>
                  {% endfor %}
                </td>
                <td>{% for item in case.dependency.blocked_by[:4] %}<div class="meta">{{ item }}</div>{% endfor %}</td>
                <td>{{ fmt(case.updated_at) }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>

        <div class="panel work-queue">
          <h2>Action Queue</h2>
          <div class="meta">人が見るべきrunです。Next actionは「何について何を判断するか」、Review targetは「どのカテゴリーの文書を見るか」を示します。</div>
          <table>
            <thead><tr><th>Run</th><th>Ticket</th><th>Status</th><th>Next action</th><th>Review target</th><th>Updated</th></tr></thead>
            <tbody>
            {% for run in work_runs %}
              <tr>
                <td><a href="{{ link('web_run_detail', run_id=run.id) }}">{{ run.id }}</a><div class="meta">{{ run.environment }} / {{ run.machine }}</div></td>
                <td>{{ run.ticket_id or "" }}</td>
                <td><span class="badge badge-{{ run.status }}" title="{{ run_status_help }}">{{ run.status }}</span></td>
                <td><strong>{{ run.action.queue }}</strong><br><span class="meta">{{ run.action.next_action }}</span></td>
                <td>{{ run.action.review_target }}</td>
                <td>{{ fmt(run.updated_at) }}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>
        </div>

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
              <a href="{{ link('web_runs', status='routing_requested') }}" title="調査要求をDB-firstで分解するworker待ち">routing {{ counts.routing_requested_runs }}</a>
              <a href="{{ link('web_runs', status='investigation_waiting') }}" title="子taskや方針確認、実機結果の到着待ち">waiting {{ counts.investigation_waiting_runs }}</a>
              <a href="{{ link('web_runs', status='knowledge_researching') }}" title="Knowledge/DB検索workerが処理中">knowledge researching {{ counts.knowledge_researching_runs }}</a>
              <a href="{{ link('web_runs', status='planning') }}" title="runbook workerが計画生成中の調査run">planning {{ counts.planning_runs }}</a>
              <a href="{{ link('web_runs', status='review_requested') }}" title="runbook-plan文書が添付され、risk/technical評価待ちの調査run">review requested {{ counts.review_requested_runs }}</a>
              <a href="{{ link('web_runs', status='revision_requested') }}" title="risk/technical評価からplan修正へ差し戻された調査run">revision requested {{ counts.revision_requested_runs }}</a>
              <a href="{{ link('web_runs', status='review_passed') }}" title="risk/technical評価を通過し、人間の実行前確認へ進める調査run">review passed {{ counts.review_passed_runs }}</a>
              <a href="{{ link('web_runs', status='task_done') }}" title="子taskの成果物が親caseで消費可能">task done {{ counts.task_done_runs }}</a>
              <a href="{{ link('web_runs', status='result_registered') }}" title="実行結果登録済みで回答合成待ち">result registered {{ counts.result_registered_runs }}</a>
              <a href="{{ link('web_runs', status='answer_review') }}" title="回答案の人間レビュー待ち">answer review {{ counts.answer_review_runs }}</a>
              <a href="{{ link('web_runs', status='policy_review') }}" title="運用方針の人間判断待ち">policy {{ counts.policy_review_runs }}</a>
              <a href="{{ link('web_runs', status='human_review') }}" title="その他の人間判断待ち">human {{ counts.human_review_runs }}</a>
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
        runnable_counts=runnable_data["counts"],
        cases=_load_case_dashboard(),
        work_runs=[_run_row_to_dict(row) | {"action": _run_list_action(_run_row_to_dict(row))} for row in work_runs],
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
            <a href="{{ link('web_documents', q='case-decision') }}" title="既存caseに文脈追加するか新規case/taskにするかの判断を探します">case decisions</a>
            <a href="{{ link('web_documents', q='runbook-risk-review') }}" title="実機操作前のrisk評価文書を探します">risk reviews</a>
            <a href="{{ link('web_documents', q='runbook-technical-review') }}" title="技術評価・既知問題確認文書を探します">technical reviews</a>
            <a href="{{ link('web_documents', q='runbook-chief-review') }}" title="risk/technical査読を統合した主査レビューを探します">chief reviews</a>
            <a href="{{ link('web_documents', q='runbook-revision-request') }}" title="runbook planの差し戻し依頼を探します">revision requests</a>
            <a href="{{ link('web_documents', q='investigation-router-plan') }}" title="DB-firstの調査分解結果を探します">router plans</a>
            <a href="{{ link('web_documents', q='real-machine-investigation-request') }}" title="実機調査要求を探します">real-machine requests</a>
            <a href="{{ link('web_documents', q='knowledge-research-request') }}" title="DB/Knowledge検索要求を探します">knowledge requests</a>
            <a href="{{ link('web_documents', q='policy-decision-request') }}" title="方針判断要求を探します">policy requests</a>
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


def _load_runnable_runs(*, limit: int = 200) -> dict[str, Any]:
    statuses = sorted(
        set(HUMAN_REQUIRED_STATUSES)
        | set(AI_WORKER_IN_PROGRESS)
        | {"executing", "review_passed", "execution_failed"}
        | {TASK_DONE_STATUS}
        | {status for spec in AI_WORKER_RUNNABLES.values() for status in spec["statuses"]}
    )
    params = {f"status_{i}": status for i, status in enumerate(statuses)}
    params["limit"] = int(limit)
    placeholders = ", ".join(f":status_{i}" for i in range(len(statuses)))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
            WHERE r.status IN ({placeholders})
            GROUP BY r.id
            ORDER BY
              CASE
                WHEN r.status IN ('routing_requested', 'investigation_waiting', 'requested', 'revision_requested', 'review_requested', 'planned', 'result_registered') THEN 0
                WHEN r.status = 'review_passed' THEN 1
                WHEN r.status IN ('answer_review', 'policy_review', 'human_review') THEN 2
                WHEN r.status IN ('routing', 'knowledge_researching', 'planning', 'risk_reviewing', 'technical_reviewing', 'answer_synthesizing', 'executing') THEN 3
                WHEN r.status IN ('task_done', 'closed', 'done') THEN 4
                ELSE 4
              END,
              r.updated_at ASC
            LIMIT :limit
            """,
            params,
        ).fetchall()
    groups: dict[str, list[dict[str, Any]]] = {
        "ai_worker_claimable": [],
        "real_machine_claimable": [],
        "human_required": [],
        "in_progress_ai_worker": [],
        "in_progress_real_machine": [],
        "failed_or_blocked": [],
        "completed_task": [],
        "not_runnable": [],
    }
    for row in rows:
        run = _run_row_to_dict(row)
        run["document_count"] = row["document_count"]
        run["runnable_bucket"] = _runnable_bucket(run)
        run["worker_targets"] = _runnable_worker_targets(run)
        run["dependency"] = _run_dependency_context(run)
        run["action"] = _run_list_action(run)
        groups.setdefault(run["runnable_bucket"], []).append(run)
    return {
        "groups": groups,
        "counts": {name: len(runs) for name, runs in groups.items()},
        "worker_specs": AI_WORKER_RUNNABLES,
    }


@app.get("/runs/runnable")
@app.get("/knowledge/runs/runnable")
def web_runnable_runs():
    _init_db()
    data = _load_runnable_runs(limit=200)
    return _render(
        "Runnable Runs",
        """
        <h1>Runnable Runs</h1>
        <div class="panel">
          <div class="meta">DB-first worker/agent viewです。AI workerがclaim可能なrun、実機agent/人間がclaim可能なrun、人間判断待ち、処理中leaseを分けて表示します。</div>
        </div>

        <div class="panel">
          <h2>Worker Claim Map</h2>
          <table>
            <thead><tr><th>Worker</th><th>Claimable statuses</th><th>Task type filter</th><th>Claim status</th><th>Role</th></tr></thead>
            <tbody>
              {% for name, spec in worker_specs.items() %}
              <tr>
                <td><code>{{ name }}</code></td>
                <td>{{ spec.statuses | join(", ") }}</td>
                <td>{{ (spec.task_types or ["any"]) | join(", ") }}</td>
                <td><code>{{ spec.claim_status }}</code></td>
                <td>{{ spec.description }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>

        {% for bucket, title in [
          ("ai_worker_claimable", "AI Worker Claimable"),
          ("real_machine_claimable", "Real-Machine Claimable"),
          ("human_required", "Human Required"),
          ("in_progress_ai_worker", "AI Worker In Progress"),
          ("in_progress_real_machine", "Real-Machine In Progress"),
          ("failed_or_blocked", "Failed Or Blocked"),
          ("completed_task", "Completed Child Tasks")
        ] %}
          <div class="panel">
            <h2>{{ title }} <span class="meta">{{ counts.get(bucket, 0) }}</span></h2>
            <table>
              <thead><tr><th>Run</th><th>Task</th><th>Status</th><th>Targets / Claim</th><th>Blocked By</th><th>Unblocks When</th><th>Next action</th><th>Updated</th></tr></thead>
              <tbody>
              {% for run in groups.get(bucket, []) %}
                <tr>
                  <td><a href="{{ link('web_run_detail', run_id=run.id) }}">{{ run.id }}</a>{% if run.parent_run_id %}<br><span class="meta">parent <a href="{{ link('web_run_detail', run_id=run.parent_run_id) }}">{{ run.parent_run_id }}</a></span>{% endif %}<br><span class="meta">ticket={{ run.ticket_id or "" }} {{ run.environment }} / {{ run.machine }}</span></td>
                  <td>{{ run.task_type or "run" }}{% if run.required_capabilities %}<br><span class="meta">{{ run.required_capabilities | join(", ") }}</span>{% endif %}</td>
                  <td><span class="badge badge-{{ run.status }}" title="{{ run_status_help }}">{{ run.status }}</span></td>
                  <td>
                    {% if run.worker_targets %}<span class="meta">workers: {{ run.worker_targets | join(", ") }}</span>{% endif %}
                    {% if run.worker_claimed_by %}<span class="meta">worker claim={{ run.worker_claimed_by }} until {{ fmt(run.worker_lease_until) }}</span>{% endif %}
                    {% if run.claimed_by %}<span class="meta">exec claim={{ run.claimed_by }} until {{ fmt(run.lease_until) }}</span>{% endif %}
                  </td>
                  <td>{% for item in run.dependency.blocked_by[:4] %}<div class="meta">{{ item }}</div>{% endfor %}</td>
                  <td>{% for item in run.dependency.unblocks_when[:4] %}<div class="meta">{{ item }}</div>{% endfor %}</td>
                  <td><strong>{{ run.action.queue }}</strong><br><span class="meta">{{ run.action.next_action }}</span></td>
                  <td>{{ fmt(run.updated_at) }}</td>
                </tr>
              {% endfor %}
              </tbody>
            </table>
          </div>
        {% endfor %}
        """,
        groups=data["groups"],
        counts=data["counts"],
        worker_specs=data["worker_specs"],
        run_status_help=RUN_STATUS_HELP,
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
    parent_run_id = str(request.args.get("parent_run_id") or "").strip()
    task_type = str(request.args.get("task_type") or "").strip()
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
    if parent_run_id:
        filters.append("r.parent_run_id = :parent_run_id")
        params["parent_run_id"] = parent_run_id
    if task_type:
        filters.append("r.task_type = :task_type")
        params["task_type"] = task_type
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
    for run in runs:
        run["action"] = _run_list_action(run)
    return _render(
        "Runs",
        """
        <h1>Runs</h1>
        <div class="panel work-queue">
          <h2>Work Queue</h2>
          <div class="meta">この一覧は「どのrunで、どの種類の文書を、何の判断のために見るか」を優先して表示します。本文は各run詳細のAction RequiredとDocumentsから確認します。</div>
        </div>
        <div class="panel">
          <div class="quick-links">
            <a href="{{ link('web_runs') }}" title="すべての調査runを表示します">all</a>
            <a href="{{ link('web_runs', status='requested') }}" title="runbook workerまたは人間の処理待ちの調査run">requested</a>
            <a href="{{ link('web_runs', status='routing_requested') }}" title="調査要求をDB-firstで分解するworker待ち">routing requested</a>
            <a href="{{ link('web_runs', status='investigation_waiting') }}" title="子taskや方針確認、実機結果の到着待ち">investigation waiting</a>
            <a href="{{ link('web_runs', status='knowledge_researching') }}" title="Knowledge/DB検索workerが処理中">knowledge researching</a>
            <a href="{{ link('web_runs', status='planning') }}" title="runbook workerが計画生成中の調査run">planning</a>
            <a href="{{ link('web_runs', status='review_requested') }}" title="runbook-plan文書が添付され、risk/technical評価待ちの調査run">review requested</a>
            <a href="{{ link('web_runs', status='risk_reviewing') }}" title="risk評価AIが確認中の調査run">risk reviewing</a>
            <a href="{{ link('web_runs', status='technical_reviewing') }}" title="technical評価AIが確認中の調査run">technical reviewing</a>
            <a href="{{ link('web_runs', status='revision_requested') }}" title="risk/technical評価からplan修正へ差し戻された調査run">revision requested</a>
            <a href="{{ link('web_runs', status='review_passed') }}" title="risk/technical評価を通過した調査run">review passed</a>
            <a href="{{ link('web_runs', status='executing') }}" title="実機AIまたは人間がclaimして実行中の調査run">executing</a>
            <a href="{{ link('web_runs', status='task_done') }}" title="子task成果物が親caseで消費可能">task done</a>
            <a href="{{ link('web_runs', status='result_registered') }}" title="実行結果登録済みで回答合成待ち">result registered</a>
            <a href="{{ link('web_runs', status='answer_review') }}" title="合成回答案の人間レビュー待ち">answer review</a>
            <a href="{{ link('web_runs', status='policy_review') }}" title="運用方針の人間判断待ち">policy review</a>
            <a href="{{ link('web_runs', status='human_review') }}" title="その他の人間判断待ち">human review</a>
            <a href="{{ link('web_runs', status='execution_failed') }}" title="実行失敗として停止した調査run">execution failed</a>
          </div>
          <div class="meta">Runs are investigation cases or investigation tasks. Only real_machine tasks carry executable runbooks. The actual texts live in attached Documents: runbook-plan, knowledge-research-result, findings, issue_on_run, summary, and answer_draft.</div>
        </div>
        <form method="get" class="panel">
          <input name="status" value="{{ status }}" placeholder="status">
          <input name="ticket_id" value="{{ ticket_id }}" placeholder="ticket_id">
          <input name="environment" value="{{ environment }}" placeholder="environment">
          <input name="machine" value="{{ machine }}" placeholder="machine">
          <input name="parent_run_id" value="{{ parent_run_id }}" placeholder="parent_run_id">
          <input name="task_type" value="{{ task_type }}" placeholder="task_type">
          <button type="submit">Filter</button>
        </form>
        <table>
          <thead><tr><th>ID</th><th>Ticket</th><th>Task</th><th>Environment</th><th>Machine</th><th>Status</th><th>Next action</th><th>Review target</th><th>Summary</th><th>Docs</th><th>Updated</th></tr></thead>
          <tbody>
          {% for run in runs %}
            <tr>
              <td><a href="{{ link('web_run_detail', run_id=run.id) }}">{{ run.id }}</a>{% if run.parent_run_id %}<br><span class="meta">parent <a href="{{ link('web_run_detail', run_id=run.parent_run_id) }}">{{ run.parent_run_id }}</a></span>{% endif %}</td>
              <td>{{ run.ticket_id or "" }}</td>
              <td>{{ run.task_type or "run" }}{% if run.required_capabilities %}<br><span class="meta">{{ run.required_capabilities | join(", ") }}</span>{% endif %}{% if run.executor_mode %}<br><span class="meta">{{ run.executor_mode }} / {{ run.risk_level }}</span>{% endif %}</td>
              <td>{{ run.environment }}</td>
              <td>{{ run.machine }}</td>
              <td><span class="badge badge-{{ run.status }}" title="{{ run_status_help }}">{{ run.status }}</span>{% if run.claimed_by %}<br><span class="meta">claimed by {{ run.claimed_by }} until {{ fmt(run.lease_until) }}</span>{% endif %}</td>
              <td><strong>{{ run.action.queue }}</strong><br><span class="meta">{{ run.action.next_action }}</span></td>
              <td>{{ run.action.review_target }}<br><span class="meta">{{ run.document_kinds }}</span></td>
              <td>{{ run.summary }}</td>
              <td>{{ run.document_count }}</td>
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
        parent_run_id=parent_run_id,
        task_type=task_type,
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


def _run_list_action(run: dict[str, Any]) -> dict[str, str]:
    status = str(run.get("status") or "")
    if status == "routing_requested":
        return {
            "queue": "Investigation routing",
            "next_action": "DB-firstで調査要求をknowledge/実機/方針確認へ分解するworker待ち",
            "review_target": "case body / answer evaluation / decision docs",
        }
    if status == "routing":
        return {
            "queue": "Investigation routing",
            "next_action": "調査要求の分解処理中",
            "review_target": "case body / answer evaluation / investigation-router-plan",
        }
    if status == "investigation_waiting":
        return {
            "queue": "Waiting investigation",
            "next_action": "Knowledge調査worker、子task、方針確認、または実機結果の到着待ち",
            "review_target": "task runs / investigation requests",
        }
    if status == "knowledge_researching":
        return {
            "queue": "Knowledge research",
            "next_action": "Knowledge/DB/RAG/Web候補検索中",
            "review_target": "knowledge-research-request / knowledge-research-result",
        }
    if status == "result_registered":
        return {
            "queue": "Answer synthesis",
            "next_action": "実行成果物から回答案を合成・評価するworker待ち",
            "review_target": "findings / issue_on_run / summary / answer_draft",
        }
    if status == "answer_synthesizing":
        return {
            "queue": "Answer synthesis",
            "next_action": "回答案の合成・質問充足評価中",
            "review_target": "findings / answer_draft",
        }
    if status == "answer_review":
        return {
            "queue": "Answer review",
            "next_action": "合成回答案を確認し、Zendesk下書き・追加調査・保留・終了を判断",
            "review_target": "answer-question-evaluation / answer_draft / findings",
        }
    if status == "policy_review":
        return {
            "queue": "Policy review",
            "next_action": "DBや実機調査では決まらない運用方針を人が判断",
            "review_target": "policy-decision-request",
        }
    if status == "human_review":
        return {
            "queue": "Human review",
            "next_action": "自動処理では進められない判断・例外・障害を確認",
            "review_target": "operator notes / issue_on_run",
        }
    if status == "review_passed":
        return {
            "queue": "Ready for execution",
            "next_action": "実機作業者またはgatewayがclaimしてrunbookを実行",
            "review_target": "runbook-plan",
        }
    if status == "revision_requested":
        return {
            "queue": "Needs revision",
            "next_action": "差し戻し指摘を反映したrunbook再生成を待つ、または人間が補足",
            "review_target": "runbook-revision-request / human-revision-request",
        }
    if status in {"review_requested", "risk_reviewing", "technical_reviewing"}:
        return {
            "queue": "AI review",
            "next_action": "risk / technical / chief reviewの完了待ち",
            "review_target": "runbook-plan",
        }
    if status in {"requested", "planning"}:
        return {
            "queue": "Planning",
            "next_action": "runbook生成待ち、machine/environment不足なら補足",
            "review_target": "ticket context / run metadata",
        }
    if status == "executing":
        return {
            "queue": "In execution",
            "next_action": "claim保持者がheartbeat、結果登録、またはrelease",
            "review_target": "runbook-plan / execution-result",
        }
    if status == "execution_failed":
        return {
            "queue": "Failed execution",
            "next_action": "失敗理由を確認し、追加investigation taskか人間保留に分岐",
            "review_target": "issue_on_run / findings",
        }
    if status == "task_done":
        return {
            "queue": "Completed task",
            "next_action": "子task成果物は親caseで消費可能。必要なら親case側で回答合成や追加task判断を行う",
            "review_target": "task result documents",
        }
    if status == "closed":
        return {
            "queue": "Closed",
            "next_action": "完了済み。必要なら文書だけ参照",
            "review_target": "final documents",
        }
    return {
        "queue": "Other",
        "next_action": "状態を確認",
        "review_target": "attached documents",
    }


def _run_action_context(
    run: dict[str, Any],
    documents: list[dict[str, Any]],
    review_focus: dict[str, Any],
    execution_results: dict[str, Any],
) -> dict[str, Any]:
    action = _run_list_action(run)
    latest_by_kind = {kind: _latest_document(documents, kind) for kind in (
        "runbook-plan",
        "runbook-chief-review",
        "runbook-revision-request",
        "human-revision-request",
        "answer-question-evaluation",
        "case-decision",
        "investigation-routing-request",
        "investigation-router-plan",
        "knowledge-research-request",
        "knowledge-research-result",
        "real-machine-investigation-request",
        "policy-decision-request",
        "operator-note",
        "findings",
        "issue_on_run",
        "summary",
        "answer_draft",
    )}

    def package_doc(kind: str, label: str, why: str) -> dict[str, Any] | None:
        document = latest_by_kind.get(kind)
        if not document:
            return None
        body = _markdown_body_without_leading_meta(str(document.get("body_md") or ""))
        return {
            "kind": kind,
            "label": label,
            "why": why,
            "document": document,
            "body": body,
            "points": _brief_items(body, limit=5, width=280),
        }

    primary_docs: list[dict[str, Any]] = []
    if run.get("status") == "answer_review":
        primary_docs = [
            {"label": "Answer draft", "why": "Zendeskへ戻せる文案か確認", "document": latest_by_kind["answer_draft"]},
            {"label": "Findings", "why": "文案の根拠になる確認済み事実", "document": latest_by_kind["findings"]},
            {"label": "Issue on run", "why": "未確認・失敗・制約を踏み越えていないか確認", "document": latest_by_kind["issue_on_run"]},
            {"label": "Summary", "why": "調査全体の短い結論", "document": latest_by_kind["summary"]},
        ]
        decisions = [
            "answer_draftをZendesk返信に使えるか判断",
            "findingsとanswer_draftの対応を確認",
            "issue_on_runに未解決の重要事項があれば追加investigation taskまたはhuman holdへ分岐",
            "LLMの言い過ぎ・未確認の断定・環境固有知識の欠落を指摘",
        ]
    elif run.get("status") == "review_passed":
        primary_docs = [
            {"label": "Runbook plan", "why": "実機作業者/gatewayが実行する計画", "document": latest_by_kind["runbook-plan"]},
            {"label": "Chief review", "why": "許可範囲・停止条件・集める根拠", "document": latest_by_kind["runbook-chief-review"]},
        ]
        decisions = [
            "gatewayでclaimしてrunbookを取得",
            "許可された読み取り系確認だけを実行",
            "findings / issue_on_run / summary / answer_draftを登録",
        ]
    elif run.get("status") == "revision_requested":
        primary_docs = [
            {"label": "Revision request", "why": "runbookに反映すべき主査・人間の指摘", "document": latest_by_kind["runbook-revision-request"]},
            {"label": "Human request", "why": "人間がまとめて指定したMust Fix", "document": latest_by_kind["human-revision-request"]},
            {"label": "Runbook plan", "why": "修正対象の計画", "document": latest_by_kind["runbook-plan"]},
        ]
        decisions = [
            "Must Fixがrunbook再生成に渡っているか確認",
            "必要なら人間が補足指示を追加",
        ]
    elif run.get("status") in {"routing_requested", "routing"}:
        primary_docs = [
            {"label": "Answer evaluation", "why": "追加調査が必要になった未回答論点", "document": latest_by_kind["answer-question-evaluation"]},
            {"label": "Case decision", "why": "既存caseへattachした理由や差分", "document": latest_by_kind["case-decision"]},
            {"label": "Router plan", "why": "分解結果と作成されたrequest", "document": latest_by_kind["investigation-router-plan"]},
        ]
        decisions = [
            "router workerの処理を待つ",
            "対象machineやscopeが不明ならhuman_reviewへ回す",
        ]
    elif run.get("status") == "investigation_waiting":
        primary_docs = [
            {"label": "Router plan", "why": "どの子task/方針確認が作られたか", "document": latest_by_kind["investigation-router-plan"]},
            {"label": "Knowledge request", "why": "DB-firstで調べる対象", "document": latest_by_kind["knowledge-research-request"]},
            {"label": "Knowledge result", "why": "既存DB/RAG/Web候補の検索結果", "document": latest_by_kind["knowledge-research-result"]},
            {"label": "Real machine request", "why": "実機側に渡した確認依頼", "document": latest_by_kind["real-machine-investigation-request"]},
            {"label": "Policy request", "why": "人が方針判断する依頼", "document": latest_by_kind["policy-decision-request"]},
        ]
        decisions = [
            "investigation taskの完了を待つ",
            "方針確認が必要ならpolicy_reviewへ分ける",
        ]
    elif run.get("status") == "knowledge_researching":
        primary_docs = [
            {"label": "Knowledge request", "why": "DB-firstで調べる対象", "document": latest_by_kind["knowledge-research-request"]},
            {"label": "Knowledge result", "why": "既存DB/RAG/Web候補の検索結果", "document": latest_by_kind["knowledge-research-result"]},
            {"label": "Router plan", "why": "なぜDB検索になったか", "document": latest_by_kind["investigation-router-plan"]},
        ]
        decisions = [
            "Knowledge research workerの処理を待つ",
            "結果が古い/条件不明なら実機調査または方針確認に分ける",
        ]
    elif run.get("status") == "result_registered":
        primary_docs = [
            {"label": "Findings", "why": "回答合成の入力になる確認済み事実", "document": latest_by_kind["findings"]},
            {"label": "Issue on run", "why": "未解決・制約・停止理由", "document": latest_by_kind["issue_on_run"]},
            {"label": "Answer draft", "why": "実行者が残した生の草稿", "document": latest_by_kind["answer_draft"]},
        ]
        decisions = [
            "answer synthesis workerの処理を待つ",
            "成果物が不足していれば実行者へ差し戻す",
        ]
    elif run.get("status") == "task_done":
        primary_docs = [
            {"label": "Knowledge result", "why": "親caseが消費できるDB/Knowledge調査結果", "document": latest_by_kind["knowledge-research-result"]},
            {"label": "Knowledge request", "why": "この子taskが何を調べたか", "document": latest_by_kind["knowledge-research-request"]},
            {"label": "Summary", "why": "子taskの短い結論", "document": latest_by_kind["summary"]},
            {"label": "Findings", "why": "確認済み事実がある場合の根拠", "document": latest_by_kind["findings"]},
        ]
        decisions = [
            "親caseのdependencyで完了済みとして扱う",
            "この結果だけで回答できるかは親caseのanswer synthesis/evaluationで判断する",
            "古い・条件不明・不足があれば親case側で追加taskを作る",
        ]
    elif run.get("status") == "policy_review":
        primary_docs = [
            {"label": "Policy request", "why": "DBや実機では決まらない運用判断", "document": latest_by_kind["policy-decision-request"]},
            {"label": "Router plan", "why": "なぜ方針判断になったか", "document": latest_by_kind["investigation-router-plan"]},
        ]
        decisions = [
            "運用方針として回答可能か判断",
            "方針決定をdocumentとして残す",
        ]
    elif run.get("status") == "human_review":
        primary_docs = [
            {"label": "Issue on run", "why": "自動処理が止まった理由", "document": latest_by_kind["issue_on_run"]},
            {"label": "Operator note", "why": "人間判断に回した根拠", "document": latest_by_kind["operator-note"]},
        ]
        decisions = [
            "例外判断、保留、追加調査、終了のいずれかを選ぶ",
        ]
    else:
        primary_docs = [
            {"label": "Runbook plan", "why": "このrunの中心文書", "document": latest_by_kind["runbook-plan"]},
            {"label": "Chief review", "why": "レビュー結果の統合", "document": latest_by_kind["runbook-chief-review"]},
            {"label": "Answer draft", "why": "Zendeskへ戻す候補文", "document": latest_by_kind["answer_draft"]},
        ]
        decisions = [action["next_action"]]

    primary_docs = [item for item in primary_docs if item.get("document")]
    operator_package = [
        package_doc("answer-question-evaluation", "Answer question evaluation", "元質問に答えているかのLLM評価。人間レビューの入口。"),
        package_doc("answer_draft", "Answer draft", "Zendeskへ戻す候補文。まずこれを読む。"),
        package_doc("findings", "Findings", "回答案の根拠となる確認済み事実。"),
        package_doc("issue_on_run", "Issue on run", "未確認事項・停止理由・実行上の制約。"),
        package_doc("summary", "Summary", "調査全体の短いまとめ。"),
    ]
    return {
        **action,
        "decisions": decisions,
        "primary_docs": primary_docs,
        "operator_package": [item for item in operator_package if item],
        "has_execution_results": bool(execution_results.get("cards")),
        "chief_verdict": review_focus.get("chief_verdict") or "",
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
    action_context = _run_action_context(run, documents, review_focus, execution_results)
    return _render(
        f"Run {run_id}",
        """
        <h1>Run {{ run.id }}</h1>
        <div class="panel">
          <div class="meta">ticket={{ run.ticket_id or "" }} environment={{ run.environment }} machine={{ run.machine }} status=<span class="badge badge-{{ run.status }}" title="{{ run_status_help }}">{{ run.status }}</span> updated={{ fmt(run.updated_at) }}</div>
          <div class="meta">
            task_type={{ run.task_type or "run" }}
            {% if run.parent_run_id %} parent=<a href="{{ link('web_run_detail', run_id=run.parent_run_id) }}">{{ run.parent_run_id }}</a>{% endif %}
            {% if run.task_priority %} priority={{ run.task_priority }}{% endif %}
            {% if run.executor_mode %} executor={{ run.executor_mode }}{% endif %}
            {% if run.risk_level %} risk={{ run.risk_level }}{% endif %}
            {% if run.approval_required %} approval_required=yes{% endif %}
            {% if run.required_capabilities %} capabilities={{ run.required_capabilities | join(", ") }}{% endif %}
          </div>
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
        <div class="action-box">
          <div class="action-title">
            {% if run.status == "answer_review" %}
              Action Required: Answer Review
            {% else %}
              Action Required: {{ action_context.queue }}
            {% endif %}
          </div>
          {% if run.status == "answer_review" %}
            <p>このrunbookは実行・結果登録まで完了しています。ここではZendeskへ戻す回答案だけを確認し、返信へ進めるか、回答修正か、追加調査かを判断します。</p>
          {% else %}
            <p>{{ action_context.next_action }}</p>
          {% endif %}
          <div class="meta">Review target: {{ action_context.review_target }}{% if action_context.chief_verdict %} / chief verdict={{ action_context.chief_verdict }}{% endif %}</div>
          {% if action_context.decisions %}
          <strong>Human decision points</strong>
          <ul class="action-list">
            {% for item in action_context.decisions %}
              <li>{{ item }}</li>
            {% endfor %}
          </ul>
          {% endif %}
          {% if action_context.primary_docs %}
          <div class="doc-review-grid">
            {% for item in action_context.primary_docs %}
            <div class="doc-review-card">
              <strong>{{ item.label }}</strong>
              <a href="{{ link('web_document_detail', doc_id=item.document.id) }}">{{ item.document.title }}</a>
              <div class="meta">{{ item.why }}<br>kind={{ item.document.kind }} role={{ item.document.role }}</div>
            </div>
            {% endfor %}
          </div>
          {% endif %}
          {% if run.status == "answer_review" and action_context.operator_package %}
            {% for item in action_context.operator_package %}
              {% if item.kind == "answer-question-evaluation" %}
              <section class="priority-doc">
                <h3>{{ item.label }}: Does this answer the question?</h3>
                <div class="meta">
                  <a href="{{ link('web_document_detail', doc_id=item.document.id) }}">{{ item.document.title }}</a><br>
                  {{ item.why }} / kind={{ item.document.kind }} role={{ item.document.role }}
                </div>
                {% if item.points %}
                <ul class="issue-list">
                  {% for point in item.points %}
                    <li>{{ point }}</li>
                  {% endfor %}
                </ul>
                {% else %}
                <pre>{{ item.body }}</pre>
                {% endif %}
              </section>
              {% endif %}
            {% endfor %}
            {% for item in action_context.operator_package %}
              {% if item.kind == "answer_draft" %}
              <section class="priority-doc">
                <h3>{{ item.label }}: Zendesk reply candidate</h3>
                <div class="meta">
                  <a href="{{ link('web_document_detail', doc_id=item.document.id) }}">{{ item.document.title }}</a><br>
                  {{ item.why }} / kind={{ item.document.kind }} role={{ item.document.role }}
                </div>
                <pre>{{ item.body }}</pre>
              </section>
              {% endif %}
            {% endfor %}
            <div class="supporting-docs">
              {% for item in action_context.operator_package %}
                {% if item.kind != "answer_draft" and item.kind != "answer-question-evaluation" %}
                <section class="supporting-doc">
                  <h3>{{ item.label }}</h3>
                  <div class="meta">
                    <a href="{{ link('web_document_detail', doc_id=item.document.id) }}">{{ item.document.title }}</a><br>
                    {{ item.why }} / kind={{ item.document.kind }}
                  </div>
                  {% if item.points %}
                  <ul class="issue-list">
                    {% for point in item.points %}
                      <li>{{ point }}</li>
                    {% endfor %}
                  </ul>
                  {% else %}
                  <pre>{{ item.body }}</pre>
                  {% endif %}
                </section>
                {% endif %}
              {% endfor %}
            </div>
          {% endif %}
        </div>

        {% if run.status == "answer_review" %}
        <div class="panel work-queue">
          <h2>Answer Review Checklist</h2>
          <div class="meta">runbook planや実行前reviewは履歴です。ここでは回答案に集中してください。</div>
          <ul class="action-list">
            <li>answer_draftがfindingsに基づいているか。</li>
            <li>issue_on_runに未解決の制約があるのに断定していないか。</li>
            <li>実行していないmodule load、build、job投入、ユーザーデータ参照を確認済みとしていないか。</li>
            <li>LLMの一般論が環境固有の事実より前に出ていないか。</li>
            <li>足りない場合は文案修正ではなく、追加investigation taskまたは人間保留に分岐する。</li>
          </ul>
        </div>
        {% endif %}
        {% if run.status == "answer_review" %}
        <details class="panel">
          <summary>Closed runbook/review history</summary>
          <div class="meta">このrunbookはすでに実行前reviewを通過しています。answer判断で必要な場合だけ開いて確認します。</div>
        {% else %}
        <div class="panel">
        {% endif %}
          <h2>Runbook Review History</h2>
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
        {% if run.status == "answer_review" %}
        </details>
        {% else %}
        </div>
        {% endif %}
        {% if run.status == "answer_review" %}
        <details class="panel">
          <summary>Execution results and evidence</summary>
          <div class="meta">回答案の根拠確認が必要な場合だけ開きます。主な根拠は上のfindings / issue_on_run / summaryカードにも出ています。</div>
        {% else %}
        <div class="panel">
        {% endif %}
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
        {% if run.status == "answer_review" %}
        </details>
        {% else %}
        </div>
        {% endif %}
        {% if run.status == "answer_review" %}
        <div class="panel answer-actions">
          <h2>Answer Actions</h2>
          <div class="meta">ここで行う操作はKnowledge上の状態更新とhandoff記録だけです。Zendeskへ自動投稿はしません。</div>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="create_zendesk_draft_handoff">
            <h3>Queue Zendesk Draft</h3>
            <label>Operator note<br><textarea name="note" placeholder="この回答案をZendesk下書きとして人間確認へ進める根拠"></textarea></label>
            <div class="form-actions"><button type="submit">Create Zendesk Draft Handoff</button></div>
          </form>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="hold_answer_review">
            <h3>Hold For Human Decision</h3>
            <label>Reason<br><textarea name="note" placeholder="運用判断、公開可否、サポート範囲など、人間判断が必要な理由"></textarea></label>
            <div class="form-actions"><button type="submit">Record Hold</button></div>
          </form>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="create_real_machine_task">
            <h3>Request Additional Investigation Task</h3>
            <div class="form-grid">
              <label>Environment<br><input name="environment" value="{{ run.environment }}"></label>
              <label>Machine<br><input name="machine" value="{{ run.machine }}"></label>
            </div>
            <label>Additional scope<br><textarea name="note" placeholder="回答するには何が不足していて、次のreal-machine investigation taskで何を確認してほしいか。answer-question-evaluationの未回答論点を元に書く"></textarea></label>
            <div class="form-actions"><button type="submit">Create Additional Task</button></div>
          </form>

          <form method="post" action="{{ link('web_run_operator_action', run_id=run.id) }}" class="panel">
            <input type="hidden" name="action" value="close_run">
            <h3>Close Run</h3>
            <label>Reason<br><textarea name="note" placeholder="終了理由、別runへ統合した場合のIDなど"></textarea></label>
            <div class="form-actions"><button type="submit">Close</button></div>
          </form>
        </div>
        <details class="panel">
          <summary>Advanced runbook actions</summary>
          <div class="meta">runbook planそのものを差し戻す必要がある場合だけ使います。answer修正や追加調査とは別です。</div>

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
            <input type="hidden" name="action" value="mark_review_passed">
            <h3>Mark Review Passed</h3>
            <label>Operator note<br><textarea name="note" placeholder="人間判断で実行前確認へ進める根拠"></textarea></label>
            <div class="form-actions"><button type="submit">Mark Review Passed</button></div>
          </form>

        </details>
        {% endif %}
        {% if run.status == "answer_review" %}
        <details class="panel">
          <summary>Initial runbook request</summary>
          <pre>{{ run.runbook }}</pre>
        </details>
        {% else %}
        <h2>Runbook</h2>
        <pre>{{ run.runbook }}</pre>
        {% endif %}
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
              <label>Observed at<br><input name="observed_at" placeholder="未指定なら登録時刻"></label>
              <label>Node / target<br><input name="node" placeholder="例: c000, compute node type"></label>
              <label>Next status<br>
                <select name="next_status">
                  <option value="result_registered">result_registered</option>
                  <option value="answer_review">answer_review</option>
                  <option value="review_passed">review_passed</option>
                  <option value="task_done">task_done</option>
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
              <label>OS / image<br><input name="os_version" placeholder="例: Rocky Linux 9.x"></label>
              <label>Driver<br><input name="driver_version" placeholder="例: NVIDIA driver version"></label>
              <label>CUDA<br><input name="cuda_version" placeholder="確認したCUDA Toolkit/driver"></label>
              <label>Compiler<br><input name="compiler_version" placeholder="例: GCC version"></label>
              <label>MPI<br><input name="mpi_version" placeholder="例: HPC-X/Open MPI"></label>
              <label>Reproducibility<br>
                <select name="reproducibility">
                  <option value="unknown">unknown</option>
                  <option value="single_observation">single_observation</option>
                  <option value="reproduced">reproduced</option>
                  <option value="documented_policy">documented_policy</option>
                  <option value="historical">historical</option>
                </select>
              </label>
            </div>
            <label>Modules<br><textarea name="modules" placeholder="確認したmodule list / module avail / module showの要約"></textarea></label>
            <label>Commands<br><textarea name="commands" placeholder="実行したコマンドと、read-only / write / compile等の区別"></textarea></label>
            <label>Workdir / job conditions<br><textarea name="job_conditions" placeholder="作業ディレクトリ、ノード種別、ジョブ条件、制限、未実施事項"></textarea></label>
            <div class="form-grid">
              <label>Workdir<br><input name="workdir" placeholder="必要なら作業ディレクトリ"></label>
              <label>Reuse scope<br><input name="reuse_scope" placeholder="例: RIKYU CUDA/GCC/MPI module selection only"></label>
              <label>Stale after<br><input name="stale_after" placeholder="例: 2026-10-01 or next maintenance"></label>
              <label>Staleness triggers<br><input name="staleness_triggers" placeholder="例: driver/CUDA/HPC-X/module update"></label>
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
        action_context=action_context,
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
    if next_status and next_status != "no_change" and next_status not in {
        "result_registered",
        "answer_review",
        "policy_review",
        "human_review",
        "review_passed",
        "task_done",
        "closed",
        "execution_failed",
    }:
        raise ValueError(
            "next_status must be result_registered, answer_review, policy_review, human_review, "
            "review_passed, task_done, closed, execution_failed, or no_change"
        )
    claim_validated = _validate_claim_for_execution_result(run, payload)
    runbook_document_id = str(payload.get("runbook_document_id") or "").strip()
    runbook_title = str(payload.get("runbook_title") or "").strip()
    now_text = _fmt_ts(_now())
    observed_at = str(payload.get("observed_at") or now_text).strip()
    node = str(payload.get("node") or payload.get("node_type") or "").strip()
    os_version = str(payload.get("os_version") or "").strip()
    driver_version = str(payload.get("driver_version") or "").strip()
    cuda_version = str(payload.get("cuda_version") or "").strip()
    compiler_version = str(payload.get("compiler_version") or "").strip()
    mpi_version = str(payload.get("mpi_version") or "").strip()
    modules = str(payload.get("modules") or "").strip()
    commands = str(payload.get("commands") or "").strip()
    workdir = str(payload.get("workdir") or "").strip()
    job_conditions = str(payload.get("job_conditions") or "").strip()
    reproducibility = str(payload.get("reproducibility") or "unknown").strip() or "unknown"
    reuse_scope = str(payload.get("reuse_scope") or "").strip()
    stale_after = str(payload.get("stale_after") or "").strip()
    staleness_triggers = str(payload.get("staleness_triggers") or "").strip()

    documents: list[dict[str, Any]] = []
    for field, value in values.items():
        if not value:
            continue
        spec = EXECUTION_RESULT_FIELDS[field]
        title = f"{spec['heading']} for run {run['id']}"
        body = (
            f"# {spec['heading']}\n\n"
            f"- at: {now_text}\n"
            f"- observed_at: {observed_at}\n"
            f"- source_run_id: {run['id']}\n"
            f"- parent_run_id: {run.get('parent_run_id') or ''}\n"
            f"- task_type: {run.get('task_type') or ''}\n"
            f"- ticket_id: {run.get('ticket_id') or ''}\n"
            f"- environment: {run.get('environment') or ''}\n"
            f"- machine: {run.get('machine') or ''}\n"
            f"- node: {node}\n"
            f"- runbook_document_id: {runbook_document_id}\n"
            f"- runbook_title: {runbook_title}\n"
        )
        if field == "findings":
            body += (
                f"- os_version: {os_version}\n"
                f"- driver_version: {driver_version}\n"
                f"- cuda_version: {cuda_version}\n"
                f"- compiler_version: {compiler_version}\n"
                f"- mpi_version: {mpi_version}\n"
                f"- modules: {modules}\n"
                f"- commands: {commands}\n"
                f"- workdir: {workdir}\n"
                f"- job_conditions: {job_conditions}\n"
                f"- reproducibility: {reproducibility}\n"
                f"- reuse_scope: {reuse_scope}\n"
                f"- stale_after: {stale_after}\n"
                f"- staleness_triggers: {staleness_triggers}\n"
                "\n"
                "## Reuse / Freshness Notes\n"
                "Treat this as reusable evidence only within the recorded environment, machine, versions, modules, "
                "commands, and conditions. If those details are missing or the staleness triggers apply, re-check before using it as a basis for an answer.\n"
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
    if "status" in updates and updates["status"] not in {
        "routing",
        "knowledge_researching",
        "planning",
        "risk_reviewing",
        "technical_reviewing",
        "answer_synthesizing",
    }:
        updates["worker_claimed_by"] = ""
        updates["worker_claim_token"] = ""
        updates["worker_claimed_at"] = 0
        updates["worker_lease_until"] = 0
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{key} = :{key}" for key in updates)
    updates["id"] = run_id
    with _connect() as conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE id = :id", updates)


def _latest_run_document(run_id: str, kind: str, *, include_body: bool = False) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT d.*, rd.role, rd.created_at AS linked_at
            FROM run_documents rd
            JOIN documents d ON d.id = rd.document_id
            WHERE rd.run_id = ? AND d.kind = ?
            ORDER BY rd.created_at DESC
            LIMIT 1
            """,
            (run_id, kind),
        ).fetchone()
    if not row:
        return None
    doc = _row_to_dict(row)
    doc["role"] = row["role"]
    doc["linked_at"] = row["linked_at"]
    if include_body:
        doc["body_md"] = _document_body(row)
    return doc


def _create_document_handoff(
    *,
    document_id: str,
    run: dict[str, Any],
    channel: str,
    recipient: str,
    note: str,
) -> dict[str, Any]:
    handoff_id = str(uuid.uuid4())
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
                document_id,
                run.get("ticket_id"),
                run.get("environment") or "",
                run.get("machine") or "",
                channel,
                recipient,
                "requested",
                "",
                field_crypto.encrypt_text(note),
                now,
                now,
            ),
        )
    return {
        "id": handoff_id,
        "document_id": document_id,
        "channel": channel,
        "recipient": recipient,
        "status": "requested",
    }


def _create_run_record(payload: dict[str, Any]) -> dict[str, Any]:
    run_id = str(payload.get("id") or uuid.uuid4())
    now = _now()
    key = field_crypto.load_key()
    task_type = str(payload.get("task_type") or "").strip()
    parent_run_id = str(payload.get("parent_run_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    if not task_type:
        raise ValueError("task_type is required")
    if task_type not in ALLOWED_TASK_TYPES:
        raise ValueError(f"task_type must be one of: {', '.join(sorted(ALLOWED_TASK_TYPES))}")
    if task_type != "investigation_case" and not parent_run_id:
        raise ValueError("parent_run_id is required for investigation tasks")
    if task_type == "investigation_case" and parent_run_id:
        raise ValueError("investigation_case must not have parent_run_id")
    if not status:
        raise ValueError("status is required")
    if status not in ALLOWED_RUN_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(ALLOWED_RUN_STATUSES))}")
    capabilities = payload.get("required_capabilities") or []
    if isinstance(capabilities, str):
        capabilities = [item.strip() for item in capabilities.split(",") if item.strip()]
    if not isinstance(capabilities, list):
        capabilities = []
    record = {
        "id": run_id,
        "parent_run_id": parent_run_id,
        "ticket_id": payload.get("ticket_id"),
        "task_type": task_type,
        "task_priority": str(payload.get("task_priority") or payload.get("priority") or ""),
        "required_capabilities_json": json.dumps([str(item) for item in capabilities], ensure_ascii=False),
        "executor_mode": str(payload.get("executor_mode") or ""),
        "risk_level": str(payload.get("risk_level") or ""),
        "approval_required": 1 if bool(payload.get("approval_required")) else 0,
        "environment": str(payload.get("environment") or ""),
        "machine": str(payload.get("machine") or ""),
        "runbook": "",
        "runbook_ciphertext": field_crypto.encrypt_text(str(payload.get("runbook") or ""), key=key),
        "status": status,
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
              (id, parent_run_id, ticket_id, task_type, task_priority, required_capabilities_json,
               executor_mode, risk_level, approval_required, environment, machine, runbook, runbook_ciphertext, status,
               issue_on_run, issue_on_run_ciphertext, summary, summary_ciphertext, created_at, updated_at)
            VALUES
              (:id, :parent_run_id, :ticket_id, :task_type, :task_priority, :required_capabilities_json,
               :executor_mode, :risk_level, :approval_required, :environment, :machine, :runbook, :runbook_ciphertext, :status,
               :issue_on_run, :issue_on_run_ciphertext, :summary, :summary_ciphertext, :created_at, :updated_at)
            """,
            record,
        )
    return _run_row_to_dict(record)


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
    redirect_run_id = run_id

    if action == "create_zendesk_draft_handoff":
        answer_doc = _latest_run_document(run_id, "answer_draft")
        if not answer_doc:
            return _json_error("answer_draft document not found", 400)
        handoff = _create_document_handoff(
            document_id=str(answer_doc["id"]),
            run=run,
            channel="zendesk-draft",
            recipient="support-agent",
            note=note or "Operator requested Zendesk draft review from latest answer_draft.",
        )
        summary = "Operator queued latest answer_draft for Zendesk draft review."
        body = (
            "# Zendesk Draft Handoff Requested\n\n"
            f"- at: {now_text}\n"
            f"- answer_document_id: {answer_doc['id']}\n"
            f"- handoff_id: {handoff['id']}\n"
            "- channel: zendesk-draft\n"
            "- next_status: answer_review\n\n"
            "## Note\n"
            f"{note or 'none'}\n"
        )
        _attach_operator_note(
            run,
            title=f"Zendesk draft handoff requested for run {run_id}",
            summary=summary,
            body_md=body,
            role="zendesk_draft_handoff_request",
            kind="operator-note",
            tags=["operator-note", "zendesk-draft", "answer-action"],
        )
        _update_run_fields(run_id, {
            "summary": "",
            "_summary_plain": summary,
            "status": "answer_review",
        })
    elif action in {"create_real_machine_task", "create_additional_runbook"}:
        environment = str(request.form.get("environment") or run.get("environment") or "").strip()
        machine = str(request.form.get("machine") or run.get("machine") or "").strip()
        evaluation_doc = _latest_run_document(run_id, "answer-question-evaluation", include_body=True)
        findings_doc = _latest_run_document(run_id, "findings", include_body=True)
        issue_doc = _latest_run_document(run_id, "issue_on_run", include_body=True)
        summary_doc = _latest_run_document(run_id, "summary", include_body=True)
        answer_doc = _latest_run_document(run_id, "answer_draft", include_body=True)
        summary = "Additional investigation task requested from answer evaluation."
        initial_runbook = (
            "# Additional Investigation Task Request\n\n"
            f"- parent_run_id: {run_id}\n"
            f"- ticket_id: {run.get('ticket_id') or ''}\n"
            f"- environment: {environment}\n"
            f"- machine: {machine}\n"
            "- trigger: answer-question-evaluation indicated the current answer does not safely answer the user question\n\n"
            "## Operator Scope\n"
            f"{note or 'answer-question-evaluationのunanswered_points / missing evidenceを確認し、回答可能な根拠を追加で集める。'}\n\n"
            "## Previous Answer Evaluation\n"
            f"{str((evaluation_doc or {}).get('body_md') or 'none').strip()}\n\n"
            "## Previous Findings\n"
            f"{str((findings_doc or {}).get('body_md') or 'none').strip()}\n\n"
            "## Previous Issue On Run\n"
            f"{str((issue_doc or {}).get('body_md') or 'none').strip()}\n\n"
            "## Previous Summary\n"
            f"{str((summary_doc or {}).get('body_md') or 'none').strip()}\n\n"
            "## Previous Answer Draft\n"
            f"{str((answer_doc or {}).get('body_md') or 'none').strip()}\n\n"
            "## Required Output\n"
            "- findings: 追加確認で分かった事実。前回findingsとの差分を明記する。\n"
            "- issue_on_run: 未確認事項、止めた理由、実行しなかった操作。\n"
            "- summary: これで元質問にどこまで答えられるか。\n"
            "- answer_draft: 確認済み事実だけに基づく更新回答案。\n"
        )
        child_run = _create_run_record({
            "ticket_id": run.get("ticket_id"),
            "parent_run_id": run_id,
            "task_type": "real_machine",
            "task_priority": "normal",
            "required_capabilities": ["read_only"],
            "executor_mode": "human_with_ai",
            "risk_level": "medium",
            "approval_required": True,
            "environment": environment,
            "machine": machine,
            "status": "requested",
            "summary": summary,
            "runbook": initial_runbook,
        })
        source_body = (
            "# Additional Investigation Task Source\n\n"
            f"- at: {now_text}\n"
            f"- parent_run_id: {run_id}\n"
            f"- child_run_id: {child_run['id']}\n"
            f"- answer_evaluation_document_id: {str((evaluation_doc or {}).get('id') or '')}\n\n"
            "## Operator Scope\n"
            f"{note or 'none'}\n\n"
            "## Source Evaluation\n"
            f"{str((evaluation_doc or {}).get('body_md') or 'none').strip()}\n"
        )
        _attach_run_document(
            child_run,
            title=f"Additional investigation task source from parent case {run_id}",
            summary="Source context for additional real-machine investigation task generated from answer evaluation.",
            body_md=source_body,
            role="real_machine_investigation_source",
            kind="real-machine-investigation-source",
            tags=["real-machine-investigation", "answer-evaluation", "parent-case"],
        )
        _attach_operator_note(
            run,
            title=f"Additional investigation task requested from case {run_id}",
            summary=f"Created investigation task run {child_run['id']} from answer evaluation.",
            body_md=(
                "# Additional Investigation Task Requested\n\n"
                f"- at: {now_text}\n"
                f"- child_run_id: {child_run['id']}\n"
                f"- answer_evaluation_document_id: {str((evaluation_doc or {}).get('id') or '')}\n"
                "- next_status: investigation_waiting\n\n"
                "## Operator Scope\n"
                f"{note or 'none'}\n"
            ),
            role="real_machine_investigation_request",
            kind="operator-note",
            tags=["operator-note", "real-machine-investigation", "answer-action"],
        )
        _update_run_fields(run_id, {
            "summary": "",
            "_summary_plain": f"Additional investigation task requested: {child_run['id']}",
            "status": "investigation_waiting",
        })
        redirect_run_id = str(child_run["id"])
    elif action == "hold_answer_review":
        summary = "Operator held answer review for human decision."
        body = (
            "# Answer Review Hold\n\n"
            f"- at: {now_text}\n"
            "- next_status: human_review\n\n"
            "## Reason\n"
            f"{note or 'none'}\n"
        )
        _attach_operator_note(
            run,
            title=f"Answer review hold for run {run_id}",
            summary=summary,
            body_md=body,
            role="answer_review_hold",
            kind="operator-note",
            tags=["operator-note", "answer-review", "human-hold"],
        )
        _update_run_fields(run_id, {
            "summary": "",
            "_summary_plain": summary,
            "status": "human_review",
        })
    elif action == "set_target_revision":
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
            "status": "investigation_waiting",
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
                    "observed_at": request.form.get("observed_at"),
                    "node": request.form.get("node"),
                    "os_version": request.form.get("os_version"),
                    "driver_version": request.form.get("driver_version"),
                    "cuda_version": request.form.get("cuda_version"),
                    "compiler_version": request.form.get("compiler_version"),
                    "mpi_version": request.form.get("mpi_version"),
                    "modules": request.form.get("modules"),
                    "commands": request.form.get("commands"),
                    "workdir": request.form.get("workdir"),
                    "job_conditions": request.form.get("job_conditions"),
                    "reproducibility": request.form.get("reproducibility"),
                    "reuse_scope": request.form.get("reuse_scope"),
                    "stale_after": request.form.get("stale_after"),
                    "staleness_triggers": request.form.get("staleness_triggers"),
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
    return redirect(_web_url("web_run_detail", run_id=redirect_run_id))


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
    try:
        run = _create_run_record(payload)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    return jsonify({"ok": True, "run": run}), 201


@app.get("/api/runs")
def list_runs():
    _init_db()
    status = str(request.args.get("status") or "").strip()
    ticket_id = request.args.get("ticket_id")
    environment = str(request.args.get("environment") or "").strip()
    machine = str(request.args.get("machine") or "").strip()
    parent_run_id = str(request.args.get("parent_run_id") or "").strip()
    task_type = str(request.args.get("task_type") or "").strip()
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
    if parent_run_id:
        filters.append("r.parent_run_id = :parent_run_id")
        params["parent_run_id"] = parent_run_id
    if task_type:
        filters.append("r.task_type = :task_type")
        params["task_type"] = task_type
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


@app.get("/api/runs/runnable")
def api_runnable_runs():
    _init_db()
    limit = min(max(int(request.args.get("limit", "200")), 1), 500)
    data = _load_runnable_runs(limit=limit)
    groups = {
        name: [
            {
                "id": run.get("id"),
                "ticket_id": run.get("ticket_id"),
                "parent_run_id": run.get("parent_run_id"),
                "task_type": run.get("task_type"),
                "task_priority": run.get("task_priority"),
                "required_capabilities": run.get("required_capabilities") or [],
                "executor_mode": run.get("executor_mode"),
                "risk_level": run.get("risk_level"),
                "approval_required": run.get("approval_required"),
                "environment": run.get("environment"),
                "machine": run.get("machine"),
                "status": run.get("status"),
                "summary": run.get("summary"),
                "worker_targets": run.get("worker_targets") or [],
                "dependency": run.get("dependency") or {},
                "worker_claimed_by": run.get("worker_claimed_by"),
                "worker_lease_until": run.get("worker_lease_until"),
                "claimed_by": run.get("claimed_by"),
                "lease_until": run.get("lease_until"),
                "updated_at": run.get("updated_at"),
            }
            for run in runs
        ]
        for name, runs in data["groups"].items()
    }
    return jsonify({
        "ok": True,
        "counts": data["counts"],
        "groups": groups,
        "worker_specs": data["worker_specs"],
    })


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


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _validate_worker_claim_scope(
    worker: str,
    statuses: list[str],
    claim_status: str,
    task_type: str,
) -> tuple[list[str], str, str, str | None]:
    spec = AI_WORKER_RUNNABLES.get(worker)
    if not spec:
        return statuses, claim_status, task_type, f"unknown worker: {worker}"

    allowed_statuses = set(spec["statuses"])
    requested_statuses = set(statuses)
    if not requested_statuses:
        return statuses, claim_status, task_type, "statuses is required"
    disallowed_statuses = sorted(requested_statuses - allowed_statuses)
    if disallowed_statuses:
        return (
            statuses,
            claim_status,
            task_type,
            f"worker {worker} cannot claim statuses: {', '.join(disallowed_statuses)}",
        )

    expected_claim_status = str(spec.get("claim_status") or "")
    if claim_status != expected_claim_status:
        return (
            statuses,
            claim_status,
            task_type,
            f"worker {worker} must use claim_status={expected_claim_status}",
        )

    allowed_task_types = [str(item) for item in spec.get("task_types") or [] if str(item)]
    if allowed_task_types:
        if task_type and task_type not in allowed_task_types:
            return (
                statuses,
                claim_status,
                task_type,
                f"worker {worker} cannot claim task_type={task_type}",
            )
        if not task_type and len(allowed_task_types) == 1:
            task_type = allowed_task_types[0]
        elif not task_type:
            return (
                statuses,
                claim_status,
                task_type,
                f"worker {worker} must specify one of task_type={', '.join(allowed_task_types)}",
            )

    return statuses, claim_status, task_type, None


@app.post("/api/runs/worker-claim")
def worker_claim_run():
    _init_db()
    payload = request.get_json(silent=True) or {}
    worker = str(payload.get("worker") or payload.get("worker_id") or "").strip()
    if not worker:
        return _json_error("worker is required", 400)
    worker_name = worker.split("@", 1)[0].strip()
    statuses = _string_list(payload.get("statuses") or payload.get("status"))
    if not statuses:
        return _json_error("statuses is required", 400)
    claim_status = str(payload.get("claim_status") or statuses[0]).strip()
    if not claim_status:
        return _json_error("claim_status is required", 400)
    run_id = str(payload.get("run_id") or "").strip()
    ticket_id = payload.get("ticket_id")
    environment = str(payload.get("environment") or "").strip()
    machine = str(payload.get("machine") or "").strip()
    parent_run_id = str(payload.get("parent_run_id") or "").strip()
    task_type = str(payload.get("task_type") or "").strip()
    statuses, claim_status, task_type, scope_error = _validate_worker_claim_scope(
        worker_name,
        statuses,
        claim_status,
        task_type,
    )
    if scope_error:
        return _json_error(scope_error, 400)
    lease_seconds = _lease_seconds(payload.get("lease_seconds"))
    now = _now()
    token = str(uuid.uuid4())

    status_params = {f"status_{i}": status for i, status in enumerate(statuses)}
    status_placeholders = ", ".join(f":status_{i}" for i in range(len(statuses)))
    filters = [
        f"""
        (
          (status IN ({status_placeholders}) AND (worker_claim_token = '' OR worker_lease_until <= :now))
          OR (status = :claim_status AND worker_lease_until <= :now)
        )
        """
    ]
    params: dict[str, Any] = {**status_params, "claim_status": claim_status, "now": now}
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
    if parent_run_id:
        filters.append("parent_run_id = :parent_run_id")
        params["parent_run_id"] = parent_run_id
    if task_type:
        filters.append("task_type = :task_type")
        params["task_type"] = task_type
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
            SET status = ?,
                worker_claimed_by = ?,
                worker_claim_token = ?,
                worker_claimed_at = ?,
                worker_lease_until = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (claim_status, worker, token, now, lease_until, now, row["id"]),
        )
        updated = conn.execute("SELECT * FROM runs WHERE id = ?", (row["id"],)).fetchone()
        conn.commit()
    return jsonify({"ok": True, "run": _run_row_to_dict(updated, include_claim_token=True), "worker_claim_token": token})


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
    parent_run_id = str(payload.get("parent_run_id") or "").strip()
    task_type = str(payload.get("task_type") or "").strip()
    executor_mode = str(payload.get("executor_mode") or "").strip()
    capability = str(payload.get("capability") or "").strip()
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
    if parent_run_id:
        filters.append("parent_run_id = :parent_run_id")
        params["parent_run_id"] = parent_run_id
    if task_type:
        filters.append("task_type = :task_type")
        params["task_type"] = task_type
    if executor_mode:
        filters.append("executor_mode = :executor_mode")
        params["executor_mode"] = executor_mode
    if capability:
        filters.append("required_capabilities_json LIKE :capability")
        params["capability"] = f"%{json.dumps(capability, ensure_ascii=False)[1:-1]}%"
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
    if next_status not in {"review_passed", "result_registered", "answer_review", "task_done", "closed", "execution_failed"}:
        return _json_error(
            "next_status must be review_passed, result_registered, answer_review, task_done, closed, or execution_failed",
            400,
        )
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
    if "parent_run_id" in payload:
        updates["parent_run_id"] = str(payload["parent_run_id"])
    if "task_type" in payload:
        updates["task_type"] = str(payload["task_type"])
    if "task_priority" in payload:
        updates["task_priority"] = str(payload["task_priority"])
    if "required_capabilities" in payload:
        capabilities = payload.get("required_capabilities") or []
        if isinstance(capabilities, str):
            capabilities = [item.strip() for item in capabilities.split(",") if item.strip()]
        if not isinstance(capabilities, list):
            capabilities = []
        updates["required_capabilities_json"] = json.dumps([str(item) for item in capabilities], ensure_ascii=False)
    if "executor_mode" in payload:
        updates["executor_mode"] = str(payload["executor_mode"])
    if "risk_level" in payload:
        updates["risk_level"] = str(payload["risk_level"])
    if "approval_required" in payload:
        updates["approval_required"] = 1 if bool(payload["approval_required"]) else 0
    if "issue_on_run" in payload:
        updates["issue_on_run"] = ""
        updates["issue_on_run_ciphertext"] = field_crypto.encrypt_text(str(payload["issue_on_run"]))
    if "summary" in payload:
        updates["summary"] = ""
        updates["summary_ciphertext"] = field_crypto.encrypt_text(str(payload["summary"]))
    if "status" in updates and updates["status"] not in ALLOWED_RUN_STATUSES:
        return _json_error(f"status must be one of: {', '.join(sorted(ALLOWED_RUN_STATUSES))}", 400)
    if "task_type" in updates and updates["task_type"] not in ALLOWED_TASK_TYPES:
        return _json_error(f"task_type must be one of: {', '.join(sorted(ALLOWED_TASK_TYPES))}", 400)
    if "task_type" in updates or "parent_run_id" in updates:
        with _connect() as conn:
            current = conn.execute("SELECT task_type, parent_run_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not current:
            return _json_error("run not found", 404)
        task_type = str(updates.get("task_type", current["task_type"]) or "")
        parent_run_id = str(updates.get("parent_run_id", current["parent_run_id"]) or "")
        if task_type == "investigation_case" and parent_run_id:
            return _json_error("investigation_case must not have parent_run_id", 400)
        if task_type != "investigation_case" and not parent_run_id:
            return _json_error("parent_run_id is required for investigation tasks", 400)
    if "status" in updates and updates["status"] not in {
        "routing",
        "knowledge_researching",
        "planning",
        "risk_reviewing",
        "technical_reviewing",
        "answer_synthesizing",
    }:
        updates["worker_claimed_by"] = ""
        updates["worker_claim_token"] = ""
        updates["worker_claimed_at"] = 0
        updates["worker_lease_until"] = 0
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
