#!/usr/bin/env python3
"""Knowledge runbook planner worker.

Knowledge API の status=requested/revision_requested run を拾い、runbook生成用モデルで
実行前レビュー用の plan/risk/template document を添付する。
この worker は実機操作、Zendesk投稿、公開返信を行わない。
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

import common
import llm_client


RUNBOOK_WORKER_ENABLED = os.environ.get("SUPPORT_AI_RUNBOOK_WORKER_ENABLED", "1").lower() in ("1", "true", "yes")
RUNBOOK_MAX_REVISIONS = int(os.environ.get("SUPPORT_AI_RUNBOOK_MAX_REVISIONS", "2"))


GUARD_APPROVAL_REASONS = {
    "missing_environment": "対象 environment が未特定のため、実機確認や回答案作成の前に人間が対象環境を確認する。",
    "missing_machine": "対象 machine が未特定のため、実機確認や回答案作成の前に人間が対象マシンを確認する。",
}


def _md_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip()) or "- none"


def _append_unique(values: list[Any], item: str) -> list[str]:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if item not in normalized:
        normalized.append(item)
    return normalized


def _guard_plan(plan: dict[str, Any], *, source_run: dict[str, Any]) -> dict[str, Any]:
    guarded = dict(plan)
    approval_reasons = list(guarded.get("approval_reasons") or [])
    risk_review = list(guarded.get("risk_review") or [])
    stop_conditions = list(guarded.get("stop_conditions") or [])
    operator_notes = str(guarded.get("operator_notes") or "").strip()
    guard_notes: list[str] = []

    if not str(source_run.get("environment") or "").strip():
        guarded["requires_human_approval"] = True
        guarded["answer_draft_policy"] = "hold"
        approval_reasons = _append_unique(approval_reasons, GUARD_APPROVAL_REASONS["missing_environment"])
        stop_conditions = _append_unique(stop_conditions, "対象 environment が確定するまで実機確認・ユーザー向け回答案確定を止める。")
        guard_notes.append("environment未特定")

    if not str(source_run.get("machine") or "").strip():
        guarded["requires_human_approval"] = True
        guarded["answer_draft_policy"] = "hold"
        approval_reasons = _append_unique(approval_reasons, GUARD_APPROVAL_REASONS["missing_machine"])
        stop_conditions = _append_unique(stop_conditions, "対象 machine が確定するまで実機確認・ユーザー向け回答案確定を止める。")
        guard_notes.append("machine未特定")

    if guarded.get("requires_human_approval"):
        risk_review = _append_unique(
            risk_review,
            "このplanは実行前レビュー用であり、module load、ビルド、インストール、設定変更、ジョブ投入、ユーザーデータ参照を承認しない。",
        )

    if guarded.get("answer_draft_policy") == "hold":
        guarded["answer_draft_skeleton"] = (
            "この段階では公開返信案を確定しない。findings / issue_on_run / summary が登録された後、"
            "確認済みの環境名、提供モジュール、サポート方針、検証結果だけを根拠として回答案を作成する。"
        )

    guarded["approval_reasons"] = approval_reasons
    guarded["risk_review"] = risk_review
    guarded["stop_conditions"] = stop_conditions
    guarded["_guard_notes"] = guard_notes
    if guard_notes:
        suffix = f"Worker guard applied: {', '.join(guard_notes)}."
        guarded["operator_notes"] = f"{operator_notes}\n{suffix}".strip() if operator_notes else suffix
    return guarded


def _plan_document_body(plan: dict[str, Any], *, source_run: dict[str, Any]) -> str:
    guard_notes = plan.get("_guard_notes") or []
    return (
        "# Runbook Plan\n\n"
        f"- model: {plan.get('_model', 'unknown')}\n"
        f"- source_run_id: {source_run.get('id')}\n"
        f"- ticket_id: {source_run.get('ticket_id') or ''}\n"
        f"- environment: {source_run.get('environment') or ''}\n"
        f"- machine: {source_run.get('machine') or ''}\n"
        f"- answer_draft_policy: {plan.get('answer_draft_policy')}\n"
        f"- requires_human_approval: {'yes' if plan.get('requires_human_approval') else 'no'}\n\n"
        "## Worker Guard\n"
        f"- target_environment_known: {'yes' if str(source_run.get('environment') or '').strip() else 'no'}\n"
        f"- target_machine_known: {'yes' if str(source_run.get('machine') or '').strip() else 'no'}\n"
        f"- guard_notes: {', '.join(guard_notes) if guard_notes else 'none'}\n"
        "- approved_without_review: no\n\n"
        "## Problem Summary\n"
        f"{str(plan.get('problem_summary') or '').strip()}\n\n"
        "## Environment Scope\n"
        f"{str(plan.get('environment_scope') or '').strip()}\n\n"
        "## Knowledge Queries\n"
        f"{_md_list(plan.get('knowledge_queries') or [])}\n\n"
        "## Read-only Checks\n"
        f"{_md_list(plan.get('read_only_checks') or [])}\n\n"
        "## Risk Review\n"
        f"{_md_list(plan.get('risk_review') or [])}\n\n"
        "## Human Approval Reasons\n"
        f"{_md_list(plan.get('approval_reasons') or [])}\n\n"
        "## Stop Conditions\n"
        f"{_md_list(plan.get('stop_conditions') or [])}\n\n"
        "## Execution Steps\n"
        f"{_md_list(plan.get('execution_steps') or [])}\n\n"
        "## Findings Template\n"
        f"{str(plan.get('findings_template') or '').strip()}\n\n"
        "## Issue On Run Template\n"
        f"{str(plan.get('issue_on_run_template') or '').strip()}\n\n"
        "## Summary Template\n"
        f"{str(plan.get('summary_template') or '').strip()}\n\n"
        "## Answer Draft Skeleton\n"
        f"{str(plan.get('answer_draft_skeleton') or '').strip()}\n\n"
        "## Operator Notes\n"
        f"{str(plan.get('operator_notes') or '').strip()}\n"
    )


def _attach_plan(run: dict[str, Any], plan: dict[str, Any]) -> str:
    plan_revision_no = _next_plan_revision_no(run.get("_documents") or [])
    title = str(plan.get("title") or f"Runbook plan for {run.get('id')}")
    summary = str(plan.get("problem_summary") or run.get("summary") or title)
    created = common.knowledge_attach_run_document(str(run["id"]), {
        "role": "runbook_plan" if plan_revision_no == 0 else f"runbook_plan_r{plan_revision_no}",
        "ticket_id": run.get("ticket_id"),
        "kind": "runbook-plan",
        "title": title,
        "summary": summary,
        "body_md": _plan_document_body(plan, source_run=run),
        "tags": [
            "runbook-plan",
            "ai-generated",
            str(plan.get("answer_draft_policy") or "hold"),
            f"revision:{plan_revision_no}",
        ],
        "source": "zendesk-support-ai-runbook-worker",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    return str(document.get("id") or "")


def _revision_count(documents: list[dict[str, Any]]) -> int:
    return sum(1 for doc in documents if doc.get("kind") == "runbook-revision-request")


def _next_plan_revision_no(documents: list[dict[str, Any]]) -> int:
    """Return the next visible plan revision number.

    This is intentionally based on attached runbook-plan documents, not only
    auto-generated runbook-revision-request documents. Human revision requests
    also produce a new plan and must advance the visible role.
    """
    return sum(1 for doc in documents if doc.get("kind") == "runbook-plan")


def _review_context(documents: list[dict[str, Any]]) -> str:
    relevant = [
        doc for doc in documents
        if doc.get("kind") in {
            "human-revision-request",
            "runbook-chief-review",
            "runbook-revision-request",
            "runbook-risk-review",
            "runbook-technical-review",
        }
    ]
    if not relevant:
        return ""
    parts = []
    for doc in relevant[-6:]:
        parts.append(
            f"## {doc.get('kind')} / {doc.get('title')}\n"
            f"summary: {doc.get('summary')}\n"
            f"{str(doc.get('body_md') or '')[:8000]}"
        )
    return "\n\n".join(parts)


def process_one(run_ref: dict[str, Any], *, verbose: bool = False) -> bool:
    run_id = str(run_ref["id"])
    try:
        run = common.knowledge_get_run(run_id)
        if run.get("status") not in {"requested", "revision_requested"}:
            if verbose:
                common.log(f"runbook skip {run_id}: status={run.get('status')}")
            return False
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        revision_no = _revision_count(documents)
        if run.get("status") == "revision_requested" and revision_no > RUNBOOK_MAX_REVISIONS:
            common.knowledge_update_run(run_id, {
                "status": "operator_review",
                "issue_on_run": f"runbook revision limit exceeded: {revision_no}>{RUNBOOK_MAX_REVISIONS}",
            })
            return False
        run["_documents"] = documents
        run["review_context"] = _review_context(documents)
        common.knowledge_update_run(run_id, {"status": "planning"})
        if verbose:
            common.log(f"runbook planning {run_id}")
        plan = _guard_plan(llm_client.generate_runbook_plan(run), source_run=run)
        doc_id = _attach_plan(run, plan)
        summary = (
            f"Runbook plan generated by {plan.get('_model', 'unknown')}. "
            f"Answer policy: {plan.get('answer_draft_policy')}."
        )
        common.knowledge_update_run(run_id, {
            "status": "review_requested",
            "summary": summary,
            "issue_on_run": "",
        })
        if verbose:
            common.log(f"runbook planned {run_id}: document={doc_id}")
        return True
    except Exception as exc:  # noqa: BLE001
        common.log(f"runbook worker failed {run_id}: {exc}")
        try:
            common.knowledge_update_run(run_id, {
                "status": "operator_review",
                "issue_on_run": f"runbook worker failed: {exc}",
            })
        except Exception as update_exc:  # noqa: BLE001
            common.log(f"runbook worker failed to mark {run_id}: {update_exc}")
        return False


def run_once(verbose: bool = False, limit: int = 5) -> int:
    if not RUNBOOK_WORKER_ENABLED:
        if verbose:
            common.log("runbook worker disabled")
        return 0
    if not common.knowledge_enabled():
        if verbose:
            common.log("runbook worker skipped: Knowledge API not configured")
        return 0
    runs = common.knowledge_list_runs(status="requested", limit=limit)
    remaining = max(0, limit - len(runs))
    if remaining:
        runs.extend(common.knowledge_list_runs(status="revision_requested", limit=remaining))
    n = 0
    for run in runs:
        if process_one(run, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"runbook worker done: {len(runs)} candidate runs, {n} planned")
    return n


def run_forever(verbose: bool = False, interval: int = 60, limit: int = 5) -> None:
    common.log(f"runbook worker start (interval={interval}s limit={limit})")
    while True:
        try:
            run_once(verbose=verbose, limit=limit)
        except Exception as exc:  # noqa: BLE001
            common.log(f"runbook worker loop error (continuing): {exc}")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Runbook planner worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.once:
        run_once(verbose=args.verbose, limit=args.limit)
    else:
        run_forever(verbose=args.verbose, interval=args.interval, limit=args.limit)


if __name__ == "__main__":
    main()
