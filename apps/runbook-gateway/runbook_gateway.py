#!/usr/bin/env python3
"""Runbook gateway CLI for real-machine operators and agents."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_API_URL = os.environ.get("RUNBOOK_GATEWAY_KNOWLEDGE_API_URL", "https://fncx.r-ccs.riken.jp")
DEFAULT_TOKEN_FILE = os.environ.get("RUNBOOK_GATEWAY_TOKEN_FILE", "/run/secrets/knowledge_write_token")
DEFAULT_MACHINE = os.environ.get("RUNBOOK_GATEWAY_MACHINE", "")
DEFAULT_ENVIRONMENT = os.environ.get("RUNBOOK_GATEWAY_ENVIRONMENT", "")


def _read_file(path: str) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8").strip()


def _read_optional_text(value: str, path: str) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return value.strip()


def _token(path: str) -> str:
    token = _read_file(path)
    if not token:
        raise SystemExit(f"token file is empty or missing: {path}")
    return token


def _request(api_url: str, method: str, path: str, *, token_file: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = api_url.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token_file:
        headers["Authorization"] = "Bearer " + _token(token_file)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} -> {exc.code}: {body}") from exc


def _query(params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    return "?" + urllib.parse.urlencode(clean) if clean else ""


def _claimant(value: str) -> str:
    if value:
        return value
    user = getpass.getuser()
    uid = os.getuid()
    return f"{user}(uid:{uid})"


def _print_run(run: dict[str, Any], *, token: str = "") -> None:
    print(f"run_id={run.get('id')}")
    print(f"ticket_id={run.get('ticket_id') or ''}")
    print(f"status={run.get('status')}")
    print(f"environment={run.get('environment')}")
    print(f"machine={run.get('machine')}")
    print(f"claimed_by={run.get('claimed_by') or ''}")
    print(f"lease_until={run.get('lease_until') or ''}")
    print(f"summary={run.get('summary') or ''}")
    if token:
        print(f"claim_token={token}")


def cmd_claim(args: argparse.Namespace) -> None:
    payload = {
        "claimant": _claimant(args.claimant),
        "run_id": args.run_id,
        "status": args.status,
        "ticket_id": args.ticket_id,
        "environment": args.environment or DEFAULT_ENVIRONMENT,
        "machine": args.machine or DEFAULT_MACHINE,
        "parent_run_id": args.parent_run_id,
        "task_type": args.task_type,
        "executor_mode": args.executor_mode,
        "capability": args.capability,
        "document_kind": args.document_kind,
        "document_title_contains": args.document_title_contains,
        "document_source": args.document_source,
        "document_tag": args.document_tag,
        "lease_seconds": args.lease_seconds,
    }
    result = _request(args.api, "POST", "/api/runs/claim", token_file=args.token_file, payload=payload)
    _print_run(result["run"], token=str(result.get("claim_token") or ""))


def cmd_show(args: argparse.Namespace) -> None:
    run = _request(args.api, "GET", f"/api/runs/{args.run_id}")["run"]
    docs = _request(args.api, "GET", f"/api/runs/{args.run_id}/documents" + _query({"include_body": "1" if args.include_body else ""}))["documents"]
    if args.json:
        print(json.dumps({"run": run, "documents": docs}, ensure_ascii=False, indent=2))
        return
    _print_run(run)
    print("\n# Documents")
    for doc in docs:
        print(f"- {doc.get('role')} / {doc.get('kind')}: {doc.get('title')} ({doc.get('id')})")
    if args.include_body:
        for doc in docs:
            print(f"\n# {doc.get('role')} / {doc.get('kind')} / {doc.get('title')}\n")
            print(doc.get("body_md") or "")


def cmd_heartbeat(args: argparse.Namespace) -> None:
    result = _request(
        args.api,
        "POST",
        f"/api/runs/{args.run_id}/claim/heartbeat",
        token_file=args.token_file,
        payload={"claim_token": args.claim_token, "lease_seconds": args.lease_seconds},
    )
    _print_run(result["run"])


def cmd_release(args: argparse.Namespace) -> None:
    result = _request(
        args.api,
        "POST",
        f"/api/runs/{args.run_id}/claim/release",
        token_file=args.token_file,
        payload={"claim_token": args.claim_token, "next_status": args.next_status},
    )
    _print_run(result["run"])


def cmd_submit(args: argparse.Namespace) -> None:
    payload = {
        "source": args.source,
        "claim_token": args.claim_token,
        "runbook_document_id": args.runbook_document_id,
        "runbook_title": args.runbook_title,
        "observed_at": args.observed_at,
        "node": args.node,
        "os_version": args.os_version,
        "driver_version": args.driver_version,
        "cuda_version": args.cuda_version,
        "compiler_version": args.compiler_version,
        "mpi_version": args.mpi_version,
        "modules": args.modules,
        "commands": args.commands,
        "workdir": args.workdir,
        "job_conditions": args.job_conditions,
        "reproducibility": args.reproducibility,
        "reuse_scope": args.reuse_scope,
        "stale_after": args.stale_after,
        "staleness_triggers": args.staleness_triggers,
        "findings": _read_optional_text(args.findings, args.findings_file),
        "issue_on_run": _read_optional_text(args.issue_on_run, args.issue_on_run_file),
        "summary": _read_optional_text(args.summary, args.summary_file),
        "answer_draft": _read_optional_text(args.answer_draft, args.answer_draft_file),
        "answer_draft_policy": args.answer_draft_policy,
        "next_status": args.next_status,
        "create_zendesk_handoff": args.create_zendesk_handoff,
    }
    result = _request(
        args.api,
        "POST",
        f"/api/runs/{args.run_id}/execution-result",
        token_file=args.token_file,
        payload=payload,
    )
    _print_run(result["run"])
    print("\n# Registered documents")
    for doc in result.get("documents") or []:
        print(f"- {doc.get('kind')}: {doc.get('id')} {doc.get('title')}")
    if result.get("handoff"):
        print(f"handoff_id={result['handoff'].get('id')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runbook gateway for real-machine operators and AI agents.")
    parser.add_argument("--api", default=DEFAULT_API_URL, help="Knowledge API base URL")
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE, help="Knowledge write token file")
    sub = parser.add_subparsers(dest="command", required=True)

    claim = sub.add_parser("claim")
    claim.add_argument("--claimant", default="")
    claim.add_argument("--run-id", default="")
    claim.add_argument("--status", default="review_passed")
    claim.add_argument("--ticket-id", default="")
    claim.add_argument("--environment", default="")
    claim.add_argument("--machine", default="")
    claim.add_argument("--parent-run-id", default="")
    claim.add_argument("--task-type", default="")
    claim.add_argument("--executor-mode", default="")
    claim.add_argument("--capability", default="")
    claim.add_argument("--document-kind", default="runbook-plan")
    claim.add_argument("--document-title-contains", default="")
    claim.add_argument("--document-source", default="")
    claim.add_argument("--document-tag", default="")
    claim.add_argument("--lease-seconds", type=int, default=1800)
    claim.set_defaults(func=cmd_claim)

    show = sub.add_parser("show")
    show.add_argument("--run-id", required=True)
    show.add_argument("--include-body", action="store_true")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    heartbeat = sub.add_parser("heartbeat")
    heartbeat.add_argument("--run-id", required=True)
    heartbeat.add_argument("--claim-token", required=True)
    heartbeat.add_argument("--lease-seconds", type=int, default=1800)
    heartbeat.set_defaults(func=cmd_heartbeat)

    release = sub.add_parser("release")
    release.add_argument("--run-id", required=True)
    release.add_argument("--claim-token", required=True)
    release.add_argument(
        "--next-status",
        default="review_passed",
        choices=["review_passed", "result_registered", "answer_review", "task_done", "closed", "execution_failed"],
    )
    release.set_defaults(func=cmd_release)

    submit = sub.add_parser("submit")
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--claim-token", required=True)
    submit.add_argument("--source", default="runbook-gateway")
    submit.add_argument("--runbook-document-id", default="")
    submit.add_argument("--runbook-title", default="")
    submit.add_argument("--observed-at", default="")
    submit.add_argument("--node", default="")
    submit.add_argument("--os-version", default="")
    submit.add_argument("--driver-version", default="")
    submit.add_argument("--cuda-version", default="")
    submit.add_argument("--compiler-version", default="")
    submit.add_argument("--mpi-version", default="")
    submit.add_argument("--modules", default="")
    submit.add_argument("--commands", default="")
    submit.add_argument("--workdir", default="")
    submit.add_argument("--job-conditions", default="")
    submit.add_argument("--reproducibility", default="unknown", choices=["unknown", "single_observation", "reproduced", "documented_policy", "historical"])
    submit.add_argument("--reuse-scope", default="")
    submit.add_argument("--stale-after", default="")
    submit.add_argument("--staleness-triggers", default="")
    submit.add_argument("--findings", default="")
    submit.add_argument("--findings-file", default="")
    submit.add_argument("--issue-on-run", default="")
    submit.add_argument("--issue-on-run-file", default="")
    submit.add_argument("--summary", default="")
    submit.add_argument("--summary-file", default="")
    submit.add_argument("--answer-draft", default="")
    submit.add_argument("--answer-draft-file", default="")
    submit.add_argument("--answer-draft-policy", default="hold", choices=["hold", "internal_note", "public_reply_draft"])
    submit.add_argument(
        "--next-status",
        default="result_registered",
        choices=[
            "result_registered",
            "answer_review",
            "review_passed",
            "task_done",
            "closed",
            "execution_failed",
            "no_change",
        ],
    )
    submit.add_argument("--create-zendesk-handoff", action="store_true")
    submit.set_defaults(func=cmd_submit)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
