#!/usr/bin/env python3
"""Zendesk webhook receiver.

Webhook で受け取った ticket_id を incoming/ に積むだけの薄い入口。
後段の generator/poster は polling と同じスプールを消費する。
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any, Optional

from flask import Flask, jsonify, request

import common


app = Flask(__name__)


def _expected_token() -> str:
    return os.environ.get("TRIAGE_WEBHOOK_TOKEN", "")


def _authorized() -> bool:
    token = _expected_token()
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    header_token = request.headers.get("X-Triage-Webhook-Token", "")
    return auth == f"Bearer {token}" or header_token == token


def _find_ticket_id(payload: Any) -> Optional[int]:
    """Zendesk webhook JSON から ticket id を寛容に抽出する。"""
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("ticket_id"),
        payload.get("id"),
        (payload.get("ticket") or {}).get("id") if isinstance(payload.get("ticket"), dict) else None,
        (payload.get("ticket") or {}).get("ticket_id") if isinstance(payload.get("ticket"), dict) else None,
    ]
    for value in candidates:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def enqueue_ticket(ticket_id: int, *, source: str = "webhook") -> bool:
    """ticket_id を incoming/ に冪等に積む。新規作成したら True。"""
    common.ensure_spool_dirs()
    target = common.spool_path("incoming") / f"ticket_{ticket_id}.json"
    if target.exists():
        return False
    event = {
        "ticket_id": int(ticket_id),
        "received_at": int(time.time()),
        "source": source,
    }
    common.atomic_write_json(target, event)
    return True


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.post("/zendesk/webhook")
def zendesk_webhook():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    ticket_id = _find_ticket_id(payload)
    if ticket_id is None:
        return jsonify({"ok": False, "error": "ticket_id not found"}), 400
    queued = enqueue_ticket(ticket_id)
    return jsonify({"ok": True, "ticket_id": ticket_id, "queued": queued})


def main() -> None:
    ap = argparse.ArgumentParser(description="Zendesk triage webhook receiver")
    ap.add_argument("--host", default=os.environ.get("TRIAGE_WEBHOOK_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("TRIAGE_WEBHOOK_PORT", "8080")))
    args = ap.parse_args()
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
