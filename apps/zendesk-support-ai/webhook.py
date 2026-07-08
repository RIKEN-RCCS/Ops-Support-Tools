#!/usr/bin/env python3
"""Zendesk webhook receiver.

Webhook で受け取った ticket_id を incoming/ に積むだけの薄い入口。
後段の generator/poster は polling と同じスプールを消費する。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template_string, request, url_for
from markupsafe import Markup, escape

import common
from secret_config import env_secret


app = Flask(__name__)

MONITOR_QUEUES = ("incoming", "incoming_followup", "pending", "pending_followup", "done", "failed")
SEND_INTERNAL_NOTE_MARKER = os.environ.get("SUPPORT_AI_SEND_INTERNAL_NOTE_MARKER", "[SEND_INTERNAL_NOTE]")
INTERNAL_NOTE_SENT_TAG = os.environ.get("SUPPORT_AI_INTERNAL_NOTE_SENT_TAG", "internal_note_sent")
MAX_PUBLIC_REPLY_LEN = int(os.environ.get("SUPPORT_AI_SEND_INTERNAL_NOTE_MAX_LEN", "30000"))

BASE_CSS = """
body { color: #17202a; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f7f8fa; }
header { background: #17202a; color: white; padding: 14px 22px; }
header a { color: white; margin-right: 18px; text-decoration: none; }
main { margin: 22px auto; max-width: 1180px; padding: 0 18px; }
table { border-collapse: collapse; width: 100%; background: white; }
th, td { border-bottom: 1px solid #e2e6ea; padding: 9px 10px; text-align: left; vertical-align: top; }
th { background: #eef1f4; font-weight: 600; }
a { color: #145dbf; }
.meta { color: #66717d; font-size: 13px; }
.panel { background: white; border: 1px solid #e2e6ea; padding: 14px; margin: 14px 0; }
pre { background: #101820; color: #f4f7fb; overflow: auto; padding: 14px; white-space: pre; line-height: 1.45; tab-size: 2; }
.payload { background: white; border: 1px solid #e2e6ea; }
.payload-row { display: grid; grid-template-columns: minmax(160px, 260px) 1fr; border-top: 1px solid #e2e6ea; }
.payload-row:first-child { border-top: 0; }
.payload-key { background: #eef1f4; font-weight: 600; padding: 9px 10px; overflow-wrap: anywhere; }
.payload-value { padding: 9px 10px; overflow: auto; }
.payload-list { margin: 0; padding-left: 20px; }
.payload-text { background: #f7f8fa; color: #17202a; border: 1px solid #e2e6ea; margin: 0; white-space: pre-wrap; }
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
    <a href="{{ url_for('monitor_index') }}">Support AI Monitor</a>
    {% for q in queues %}
      <a href="{{ url_for('monitor_queue', queue=q) }}">{{ q }}</a>
    {% endfor %}
  </header>
  <main>{{ body | safe }}</main>
</body>
</html>
"""


def _render(title: str, body: str, **context: Any) -> str:
    inner = render_template_string(body, queues=MONITOR_QUEUES, **context)
    return render_template_string(LAYOUT, title=title, css=BASE_CSS, body=inner, queues=MONITOR_QUEUES)


def _fmt_ts(value: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
    except (TypeError, ValueError):
        return ""


def _expected_token() -> str:
    return env_secret("SUPPORT_AI_WEBHOOK_TOKEN")


def _authorized() -> bool:
    token = _expected_token()
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    header_token = request.headers.get("X-Support-AI-Webhook-Token", "")
    return auth == f"Bearer {token}" or header_token == token or _basic_password(auth) == token


def _basic_password(auth: str) -> str:
    if not auth.startswith("Basic "):
        return ""
    try:
        decoded = base64.b64decode(auth.removeprefix("Basic ").strip()).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
    if ":" not in decoded:
        return ""
    return decoded.split(":", 1)[1]


def _monitor_auth_required():
    return Response(
        "authentication required\n",
        401,
        {"WWW-Authenticate": 'Basic realm="Support AI Monitor"'},
    )


def _queue_items(queue: str) -> list[dict[str, Any]]:
    rows = []
    for item in common.list_queue(queue, "*.json"):
        name = item.name
        try:
            payload = common.read_json(item)
        except Exception as exc:  # noqa: BLE001
            payload = {"_read_error": str(exc)}
        rows.append(
            {
                "queue": queue,
                "name": name,
                "ticket_id": payload.get("ticket_id"),
                "comment_id": payload.get("comment_id"),
                "source": payload.get("source") or payload.get("job") or payload.get("model") or "",
                "received_at": payload.get("received_at") or payload.get("generated_at") or payload.get("accepted_at"),
                "payload": payload,
            }
        )
    return rows


def _format_payload_html(value: Any) -> Markup:
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            rows.append(
                "<div class=\"payload-row\">"
                f"<div class=\"payload-key\">{escape(str(key))}</div>"
                f"<div class=\"payload-value\">{_format_payload_html(item)}</div>"
                "</div>"
            )
        return Markup("<div class=\"payload\">" + "".join(rows) + "</div>")
    if isinstance(value, list):
        items = "".join(f"<li>{_format_payload_html(item)}</li>" for item in value)
        return Markup(f"<ol class=\"payload-list\">{items}</ol>")
    if isinstance(value, str):
        if "\n" in value:
            return Markup(f"<pre class=\"payload-text\">{escape(value)}</pre>")
        return Markup(f"<span>{escape(value)}</span>")
    if value is None:
        return Markup("<span class=\"meta\">null</span>")
    return Markup(f"<code>{escape(json.dumps(value, ensure_ascii=False))}</code>")


def _find_ticket_id(payload: Any) -> Optional[int]:
    """Zendesk webhook JSON から ticket id を寛容に抽出する。"""
    if not isinstance(payload, dict):
        return None
    subject = payload.get("subject")
    candidates = [
        payload.get("ticket_id"),
        payload.get("id"),
        (payload.get("ticket") or {}).get("id") if isinstance(payload.get("ticket"), dict) else None,
        (payload.get("ticket") or {}).get("ticket_id") if isinstance(payload.get("ticket"), dict) else None,
        (payload.get("detail") or {}).get("id") if isinstance(payload.get("detail"), dict) else None,
    ]
    for value in candidates:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    if isinstance(subject, str):
        match = re.fullmatch(r"zen:ticket:(\d+)", subject)
        if match:
            return int(match.group(1))
    return None


def _find_comment_id(payload: Any) -> Optional[int]:
    """Zendesk event/trigger payload から comment id を寛容に抽出する。"""
    if not isinstance(payload, dict):
        return None
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    comment = event.get("comment") if isinstance(event.get("comment"), dict) else {}
    candidates = [
        payload.get("comment_id"),
        (payload.get("comment") or {}).get("id") if isinstance(payload.get("comment"), dict) else None,
        comment.get("id"),
    ]
    for value in candidates:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _find_context_value(payload: Any, key: str) -> str:
    """Zendesk webhook payload から environment/machine 等の短い文脈値を抽出する。"""
    if not isinstance(payload, dict):
        return ""
    candidates = [
        payload.get(key),
        (payload.get("ticket") or {}).get(key) if isinstance(payload.get("ticket"), dict) else None,
        (payload.get("detail") or {}).get(key) if isinstance(payload.get("detail"), dict) else None,
        (payload.get("context") or {}).get(key) if isinstance(payload.get("context"), dict) else None,
    ]
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text[:120]
    return ""


def _comment_body(comment: dict[str, Any]) -> str:
    return str(comment.get("plain_body") or comment.get("body") or "").strip()


def _latest_internal_note(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    internal_notes = [
        comment
        for comment in comments
        if comment.get("public") is False and _comment_body(comment)
    ]
    return internal_notes[-1] if internal_notes else None


def _strip_send_internal_note_marker(body: str) -> tuple[bool, str]:
    lines = body.splitlines()
    if not lines or lines[0].strip() != SEND_INTERNAL_NOTE_MARKER:
        return False, body
    return True, "\n".join(lines[1:]).strip()


def enqueue_ticket(ticket_id: int, *, source: str = "webhook", environment: str = "", machine: str = "") -> bool:
    """ticket_id を incoming/ に冪等に積む。新規作成したら True。"""
    common.ensure_spool_dirs()
    name = f"ticket_{ticket_id}.json"
    if common.queue_exists("incoming", name):
        return False
    target = common.spool_path("incoming") / name
    event = {
        "ticket_id": int(ticket_id),
        "received_at": int(time.time()),
        "source": source,
        "environment": environment,
        "machine": machine,
    }
    common.atomic_write_json(target, event)
    return True


def enqueue_followup(ticket_id: int, *, comment_id: Optional[int] = None, source: str = "webhook") -> bool:
    """追加質問/追記コメント用の別キューに ticket_id を積む。"""
    common.ensure_spool_dirs()
    name = f"ticket_{ticket_id}"
    if comment_id is not None:
        name += f"_comment_{comment_id}"
    target = common.spool_path("incoming_followup") / f"{name}.json"
    if common.queue_exists("incoming_followup", target.name):
        return False
    event = {
        "ticket_id": int(ticket_id),
        "comment_id": int(comment_id) if comment_id is not None else None,
        "received_at": int(time.time()),
        "source": source,
        "job": "followup",
    }
    common.atomic_write_json(target, event)
    return True


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/support-ai/monitor")
def monitor_index():
    if not _authorized():
        return _monitor_auth_required()
    common.ensure_spool_dirs()
    counts = {queue: len(common.list_queue(queue, "*.json")) for queue in MONITOR_QUEUES}
    return _render(
        "Support AI Monitor",
        """
        <h1>Support AI Monitor</h1>
        <div class="panel">
          <p>Decrypted queue monitor for support AI workers. Use this for operational checks; persistent knowledge should be reviewed in Knowledge API.</p>
        </div>
        <table>
          <thead><tr><th>Queue</th><th>Items</th></tr></thead>
          <tbody>
          {% for queue, count in counts.items() %}
            <tr>
              <td><a href="{{ url_for('monitor_queue', queue=queue) }}">{{ queue }}</a></td>
              <td>{{ count }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        counts=counts,
    )


@app.get("/support-ai/monitor/<queue>")
def monitor_queue(queue: str):
    if not _authorized():
        return _monitor_auth_required()
    if queue not in MONITOR_QUEUES:
        return _render("Unknown queue", "<h1>Unknown queue</h1>"), 404
    common.ensure_spool_dirs()
    items = _queue_items(queue)
    return _render(
        f"Queue {queue}",
        """
        <h1>{{ queue }}</h1>
        <table>
          <thead><tr><th>Name</th><th>Ticket</th><th>Comment</th><th>Source</th><th>Time</th></tr></thead>
          <tbody>
          {% for item in items %}
            <tr>
              <td><a href="{{ url_for('monitor_queue_item', queue=queue, name=item.name) }}">{{ item.name }}</a></td>
              <td>{{ item.ticket_id or "" }}</td>
              <td>{{ item.comment_id or "" }}</td>
              <td>{{ item.source }}</td>
              <td>{{ fmt(item.received_at) }}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
        """,
        queue=queue,
        items=items,
        fmt=_fmt_ts,
    )


@app.get("/support-ai/monitor/<queue>/<name>")
def monitor_queue_item(queue: str, name: str):
    if not _authorized():
        return _monitor_auth_required()
    if queue not in MONITOR_QUEUES:
        return _render("Unknown queue", "<h1>Unknown queue</h1>"), 404
    common.ensure_spool_dirs()
    for item in _queue_items(queue):
        if item["name"] == name:
            payload = json.dumps(item["payload"], ensure_ascii=False, indent=2)
            payload_html = _format_payload_html(item["payload"])
            return _render(
                f"{queue}/{name}",
                """
                <h1>{{ queue }}/{{ name }}</h1>
                <div class="panel">
                  <div class="meta">ticket={{ item.ticket_id or "" }} comment={{ item.comment_id or "" }} source={{ item.source }}</div>
                </div>
                <h2>Readable Payload</h2>
                {{ payload_html }}
                <h2>Raw JSON</h2>
                <pre><code>{{ payload }}</code></pre>
                """,
                queue=queue,
                name=name,
                item=item,
                payload=payload,
                payload_html=payload_html,
            )
    return _render("Queue item not found", "<h1>Queue item not found</h1>"), 404


@app.post("/zendesk/webhook/triage")
def zendesk_triage_webhook():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    ticket_id = _find_ticket_id(payload)
    if ticket_id is None:
        return jsonify({"ok": False, "error": "ticket_id not found"}), 400
    environment = _find_context_value(payload, "environment")
    machine = _find_context_value(payload, "machine")
    queued = enqueue_ticket(ticket_id, environment=environment, machine=machine)
    return jsonify({
        "ok": True,
        "ticket_id": ticket_id,
        "queued": queued,
        "environment": environment,
        "machine": machine,
    })


@app.post("/zendesk/webhook/followup")
def zendesk_followup_webhook():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    ticket_id = _find_ticket_id(payload)
    if ticket_id is None:
        return jsonify({"ok": False, "error": "ticket_id not found"}), 400
    comment_id = _find_comment_id(payload)
    queued = enqueue_followup(ticket_id, comment_id=comment_id)
    return jsonify({"ok": True, "job": "followup", "ticket_id": ticket_id, "comment_id": comment_id, "queued": queued})


@app.post("/zendesk/webhook/send_internal_note")
def zendesk_send_internal_note_webhook():
    """最新の社内メモを AI なしで公開返信へ転送する。

    Zendesk trigger から ticket_id だけを受け、Ticket Comments API で最新の
    public=false comment を取得し、その本文を Ticket Update API の public=true
    comment として投稿する。
    """
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    ticket_id = _find_ticket_id(payload)
    if ticket_id is None:
        return jsonify({"ok": False, "error": "ticket_id not found"}), 400
    try:
        comments = common.fetch_ticket_comments(ticket_id)
        note = _latest_internal_note(comments)
        if not note:
            return jsonify({"ok": False, "ticket_id": ticket_id, "error": "internal note not found"}), 404
        body = _comment_body(note)
        has_marker, public_body = _strip_send_internal_note_marker(body)
        if not has_marker:
            return jsonify({
                "ok": False,
                "ticket_id": ticket_id,
                "error": "send marker not found on first line",
                "expected_marker": SEND_INTERNAL_NOTE_MARKER,
                "source_comment_id": note.get("id"),
            }), 400
        if not public_body:
            return jsonify({"ok": False, "ticket_id": ticket_id, "error": "internal note body is empty"}), 400
        if len(public_body) > MAX_PUBLIC_REPLY_LEN:
            return jsonify({
                "ok": False,
                "ticket_id": ticket_id,
                "error": "internal note body is too long",
                "max_len": MAX_PUBLIC_REPLY_LEN,
                "body_len": len(public_body),
            }), 413
        result = common.post_public_reply(
            ticket_id,
            public_body,
            tags=[INTERNAL_NOTE_SENT_TAG],
        )
        return jsonify({
            "ok": True,
            "job": "send_internal_note",
            "ticket_id": ticket_id,
            "source_comment_id": note.get("id"),
            "body_len": len(public_body),
            "marker_removed": SEND_INTERNAL_NOTE_MARKER,
            "tag": INTERNAL_NOTE_SENT_TAG,
            "zendesk_result": bool(result),
        })
    except Exception as exc:  # noqa: BLE001
        common.log(f"send_internal_note failed ticket_{ticket_id}: {exc}")
        return jsonify({"ok": False, "ticket_id": ticket_id, "error": str(exc)[:300]}), 502


def main() -> None:
    ap = argparse.ArgumentParser(description="Zendesk support AI webhook receiver")
    ap.add_argument("--host", default=os.environ.get("SUPPORT_AI_WEBHOOK_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SUPPORT_AI_WEBHOOK_PORT", "8080")))
    args = ap.parse_args()
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
