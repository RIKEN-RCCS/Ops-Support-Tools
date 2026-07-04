#!/usr/bin/env python3
"""Small helper for claiming Knowledge runs before manual execution."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def _post_json(api_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = api_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{exc.code} {exc.reason}: {body}") from exc


def _print_claim(result: dict[str, Any]) -> None:
    run = result.get("run") or {}
    print(f"run_id={run.get('id')}")
    print(f"status={run.get('status')}")
    print(f"claimed_by={run.get('claimed_by')}")
    print(f"lease_until={run.get('lease_until')}")
    if result.get("claim_token"):
        print(f"claim_token={result['claim_token']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claim or release Knowledge API runs.")
    parser.add_argument("--api", default="http://127.0.0.1:18180", help="Knowledge API base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    claim = sub.add_parser("claim", help="claim one run for manual or agent execution")
    claim.add_argument("--claimant", required=True, help="operator or agent name")
    claim.add_argument("--run-id", default="", help="specific run id to claim")
    claim.add_argument("--status", default="review_passed", help="claimable status, default: review_passed")
    claim.add_argument("--ticket-id", default="")
    claim.add_argument("--environment", default="")
    claim.add_argument("--machine", default="")
    claim.add_argument("--parent-run-id", default="")
    claim.add_argument("--task-type", default="")
    claim.add_argument("--executor-mode", default="")
    claim.add_argument("--capability", default="")
    claim.add_argument("--summary-contains", default="", help="legacy plaintext summary filter")
    claim.add_argument("--document-kind", default="", help="attached document kind, e.g. runbook-plan")
    claim.add_argument("--document-title-contains", default="")
    claim.add_argument("--document-source", default="")
    claim.add_argument("--document-tag", default="", help="attached document tag, e.g. cuda")
    claim.add_argument("--lease-seconds", type=int, default=1800)

    heartbeat = sub.add_parser("heartbeat", help="extend an active claim lease")
    heartbeat.add_argument("--run-id", required=True)
    heartbeat.add_argument("--claim-token", required=True)
    heartbeat.add_argument("--lease-seconds", type=int, default=1800)

    release = sub.add_parser("release", help="release an active claim")
    release.add_argument("--run-id", required=True)
    release.add_argument("--claim-token", required=True)
    release.add_argument(
        "--next-status",
        default="review_passed",
        choices=["review_passed", "result_registered", "answer_review", "task_done", "closed", "execution_failed"],
    )

    args = parser.parse_args()
    if args.command == "claim":
        result = _post_json(args.api, "/api/runs/claim", {
            "claimant": args.claimant,
            "run_id": args.run_id,
            "status": args.status,
            "ticket_id": args.ticket_id,
            "environment": args.environment,
            "machine": args.machine,
            "parent_run_id": args.parent_run_id,
            "task_type": args.task_type,
            "executor_mode": args.executor_mode,
            "capability": args.capability,
            "summary_contains": args.summary_contains,
            "document_kind": args.document_kind,
            "document_title_contains": args.document_title_contains,
            "document_source": args.document_source,
            "document_tag": args.document_tag,
            "lease_seconds": args.lease_seconds,
        })
    elif args.command == "heartbeat":
        result = _post_json(args.api, f"/api/runs/{args.run_id}/claim/heartbeat", {
            "claim_token": args.claim_token,
            "lease_seconds": args.lease_seconds,
        })
    else:
        result = _post_json(args.api, f"/api/runs/{args.run_id}/claim/release", {
            "claim_token": args.claim_token,
            "next_status": args.next_status,
        })
    _print_claim(result)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
