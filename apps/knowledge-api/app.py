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

from flask import Flask, jsonify, render_template_string, request, url_for

import field_crypto

DB_PATH = Path(os.environ.get("KNOWLEDGE_DB", "/data/db.sqlite"))

app = Flask(__name__)

BASE_CSS = """
body { color: #17202a; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f7f8fa; }
header { background: #17202a; color: white; padding: 14px 22px; }
header a { color: white; margin-right: 18px; text-decoration: none; }
main { margin: 22px auto; max-width: 1120px; padding: 0 18px; }
table { border-collapse: collapse; width: 100%; background: white; }
th, td { border-bottom: 1px solid #e2e6ea; padding: 9px 10px; text-align: left; vertical-align: top; }
th { background: #eef1f4; font-weight: 600; }
a { color: #145dbf; }
.meta { color: #66717d; font-size: 13px; }
.panel { background: white; border: 1px solid #e2e6ea; padding: 14px; margin: 14px 0; }
pre { background: #101820; color: #f4f7fb; overflow: auto; padding: 14px; white-space: pre-wrap; }
input, select, button { font: inherit; padding: 6px 8px; }
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
    <a href="{{ link('web_index') }}">Knowledge</a>
    <a href="{{ link('web_documents') }}">Documents</a>
    <a href="{{ link('web_runs') }}">Runs</a>
    <a href="{{ link('web_handoffs') }}">Handoffs</a>
  </header>
  <main>{{ body | safe }}</main>
</body>
</html>
"""


def _render(title: str, body: str, **context: Any) -> str:
    inner = render_template_string(body, **context)
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
        _ensure_column(conn, "document_handoffs", "note_ciphertext", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "document_handoffs", "environment", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "document_handoffs", "machine", "TEXT NOT NULL DEFAULT ''")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _json_error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


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


def _run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["runbook"] = _decrypt_field(data.get("runbook_ciphertext", ""), data.get("runbook", ""))
    data["issue_on_run"] = _decrypt_field(data.get("issue_on_run_ciphertext", ""), data.get("issue_on_run", ""))
    data["summary"] = _decrypt_field(data.get("summary_ciphertext", ""), data.get("summary", ""))
    data.pop("runbook_ciphertext", None)
    data.pop("issue_on_run_ciphertext", None)
    data.pop("summary_ciphertext", None)
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
            "requested_handoffs": conn.execute(
                "SELECT COUNT(*) AS n FROM document_handoffs WHERE status = 'requested'"
            ).fetchone()["n"],
        }
    return _render(
        "Knowledge",
        """
        <h1>Knowledge</h1>
        <div class="panel">
          <p>Encrypted SQLite knowledge store. Use the tabs above to browse decrypted records through the API process.</p>
          <ul>
            <li>Documents: {{ counts.documents }}</li>
            <li>Runs: {{ counts.runs }} / requested {{ counts.requested_runs }}</li>
            <li>Handoffs: {{ counts.handoffs }} / requested {{ counts.requested_handoffs }}</li>
          </ul>
        </div>
        """,
        counts=counts,
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
            SELECT r.*, COUNT(rd.document_id) AS document_count
            FROM runs r
            LEFT JOIN run_documents rd ON rd.run_id = r.id
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
        <form method="get" class="panel">
          <input name="status" value="{{ status }}" placeholder="status">
          <input name="ticket_id" value="{{ ticket_id }}" placeholder="ticket_id">
          <input name="environment" value="{{ environment }}" placeholder="environment">
          <input name="machine" value="{{ machine }}" placeholder="machine">
          <button type="submit">Filter</button>
        </form>
        <table>
          <thead><tr><th>ID</th><th>Ticket</th><th>Environment</th><th>Machine</th><th>Status</th><th>Summary</th><th>Docs</th><th>Updated</th></tr></thead>
          <tbody>
          {% for run in runs %}
            <tr>
              <td><a href="{{ link('web_run_detail', run_id=run.id) }}">{{ run.id }}</a></td>
              <td>{{ run.ticket_id or "" }}</td>
              <td>{{ run.environment }}</td>
              <td>{{ run.machine }}</td>
              <td>{{ run.status }}</td>
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
        fmt=_fmt_ts,
    )


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
        documents.append(doc)
    return _render(
        f"Run {run_id}",
        """
        <h1>Run {{ run.id }}</h1>
        <div class="panel">
          <div class="meta">ticket={{ run.ticket_id or "" }} environment={{ run.environment }} machine={{ run.machine }} status={{ run.status }} updated={{ fmt(run.updated_at) }}</div>
          <p>{{ run.summary }}</p>
        </div>
        <h2>Runbook</h2>
        <pre>{{ run.runbook }}</pre>
        <h2>Issue On Run</h2>
        <pre>{{ run.issue_on_run }}</pre>
        <h2>Documents</h2>
        <table>
          <thead><tr><th>Role</th><th>Title</th><th>Kind</th><th>Linked</th></tr></thead>
          <tbody>
          {% for doc in documents %}
            <tr>
              <td>{{ doc.role }}</td>
              <td><a href="{{ link('web_document_detail', doc_id=doc.id) }}">{{ doc.title }}</a></td>
              <td>{{ doc.kind }}</td>
              <td>{{ fmt(doc.linked_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        run=run,
        documents=documents,
        fmt=_fmt_ts,
    )


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
