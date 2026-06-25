#!/usr/bin/env python3
"""Runbook risk/technical reviewer worker.

Knowledge API の review_requested/planned run を拾い、runbook-plan を実行前に
risk評価とtechnical評価へ通す。必要なら revision request を添付し、
上限回数まで runbook worker に差し戻す。
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

import common
import llm_client


RUNBOOK_REVIEW_WORKER_ENABLED = os.environ.get(
    "SUPPORT_AI_RUNBOOK_REVIEW_WORKER_ENABLED", "1"
).lower() in ("1", "true", "yes")
RUNBOOK_MAX_REVISIONS = int(os.environ.get("SUPPORT_AI_RUNBOOK_MAX_REVISIONS", "2"))


def _md_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip()) or "- none"


def _revision_count(documents: list[dict[str, Any]]) -> int:
    return sum(1 for doc in documents if doc.get("kind") == "runbook-revision-request")


def _has_plan(documents: list[dict[str, Any]]) -> bool:
    return any(doc.get("kind") == "runbook-plan" for doc in documents)


def _review_body(review: dict[str, Any], *, kind: str) -> str:
    if kind == "chief":
        return (
            "# Runbook Chief Review\n\n"
            f"- model: {review.get('_model', 'unknown')}\n"
            f"- verdict: {review.get('verdict')}\n"
            f"- risk_verdict: {review.get('risk_verdict')}\n"
            f"- technical_verdict: {review.get('technical_verdict')}\n\n"
            "## Summary\n"
            f"{str(review.get('summary') or '').strip()}\n\n"
            "## Risk Points\n"
            f"{_md_list(review.get('risk_points') or [])}\n\n"
            "## Technical Points\n"
            f"{_md_list(review.get('technical_points') or [])}\n\n"
            "## Reviewer Conflicts\n"
            f"{_md_list(review.get('reviewer_conflicts') or [])}\n\n"
            "## Missing Coverage\n"
            f"{_md_list(review.get('missing_coverage') or [])}\n\n"
            "## Final Revise Requests\n"
            f"{_md_list(review.get('final_revise_requests') or [])}\n\n"
            "## Planner Patch Instructions\n"
            f"{_md_list(review.get('planner_patch_instructions') or [])}\n\n"
            "## Evidence To Collect\n"
            f"{_md_list(review.get('evidence_to_collect') or [])}\n\n"
            "## Pass Conditions\n"
            f"{_md_list(review.get('pass_conditions') or [])}\n\n"
            "## Human Decision Needed\n"
            f"{_md_list(review.get('human_decision_needed') or [])}\n\n"
            "## Operator Notes\n"
            f"{str(review.get('operator_notes') or '').strip()}\n"
        )
    if kind == "risk":
        return (
            "# Runbook Risk Review\n\n"
            f"- model: {review.get('_model', 'unknown')}\n"
            f"- verdict: {review.get('verdict')}\n"
            f"- risk_level: {review.get('risk_level')}\n"
            f"- requires_human_approval: {'yes' if review.get('requires_human_approval') else 'no'}\n\n"
            "## Summary\n"
            f"{str(review.get('summary') or '').strip()}\n\n"
            "## Unsafe Operations\n"
            f"{_md_list(review.get('unsafe_operations') or [])}\n\n"
            "## Missing Approvals\n"
            f"{_md_list(review.get('missing_approvals') or [])}\n\n"
            "## Missing Risk Controls\n"
            f"{_md_list(review.get('missing_risk_controls') or [])}\n\n"
            "## Revise Requests\n"
            f"{_md_list(review.get('revise_requests') or [])}\n\n"
            "## Stop Conditions\n"
            f"{_md_list(review.get('stop_conditions') or [])}\n\n"
            "## Operator Notes\n"
            f"{str(review.get('operator_notes') or '').strip()}\n"
        )
    return (
        "# Runbook Technical Review\n\n"
        f"- model: {review.get('_model', 'unknown')}\n"
        f"- verdict: {review.get('verdict')}\n"
        f"- confidence: {review.get('confidence')}\n"
        f"- answer_readiness: {review.get('answer_readiness')}\n\n"
        "## Summary\n"
        f"{str(review.get('summary') or '').strip()}\n\n"
        "## Missing Knowledge Queries\n"
        f"{_md_list(review.get('missing_knowledge_queries') or [])}\n\n"
        "## Known Issue Checks\n"
        f"{_md_list(review.get('known_issue_checks') or [])}\n\n"
        "## Unsupported Assumptions\n"
        f"{_md_list(review.get('unsupported_assumptions') or [])}\n\n"
        "## Revise Requests\n"
        f"{_md_list(review.get('revise_requests') or [])}\n\n"
        "## Operator Notes\n"
        f"{str(review.get('operator_notes') or '').strip()}\n"
    )


def _attach_review(run: dict[str, Any], review: dict[str, Any], *, kind: str) -> str:
    doc_kind = {
        "risk": "runbook-risk-review",
        "technical": "runbook-technical-review",
        "chief": "runbook-chief-review",
    }[kind]
    title = f"{doc_kind} for run {run.get('id')}"
    created = common.knowledge_attach_run_document(str(run["id"]), {
        "role": doc_kind,
        "ticket_id": run.get("ticket_id"),
        "kind": doc_kind,
        "title": title,
        "summary": str(review.get("summary") or title),
        "body_md": _review_body(review, kind=kind),
        "tags": [doc_kind, "ai-generated", f"verdict:{review.get('verdict')}"],
        "source": "zendesk-support-ai-runbook-review-worker",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    return str(document.get("id") or "")


def _revision_request_body(chief: dict[str, Any], *, revision_no: int) -> str:
    return (
        "# Runbook Revision Request\n\n"
        f"- revision_request_no: {revision_no}\n"
        f"- max_revisions: {RUNBOOK_MAX_REVISIONS}\n"
        f"- chief_verdict: {chief.get('verdict')}\n"
        f"- risk_verdict: {chief.get('risk_verdict')}\n"
        f"- technical_verdict: {chief.get('technical_verdict')}\n\n"
        "## Final Revise Requests\n"
        f"{_md_list(chief.get('final_revise_requests') or [])}\n\n"
        "## Planner Patch Instructions\n"
        f"{_md_list(chief.get('planner_patch_instructions') or [])}\n\n"
        "## Evidence To Collect\n"
        f"{_md_list(chief.get('evidence_to_collect') or [])}\n\n"
        "## Risk Points\n"
        f"{_md_list(chief.get('risk_points') or [])}\n\n"
        "## Technical Points\n"
        f"{_md_list(chief.get('technical_points') or [])}\n\n"
        "## Reviewer Conflicts\n"
        f"{_md_list(chief.get('reviewer_conflicts') or [])}\n\n"
        "## Missing Coverage\n"
        f"{_md_list(chief.get('missing_coverage') or [])}\n\n"
        "## Pass Conditions\n"
        f"{_md_list(chief.get('pass_conditions') or [])}\n\n"
        "## Human Decision Needed\n"
        f"{_md_list(chief.get('human_decision_needed') or [])}\n\n"
        "## Guardrail\n"
        "- Revise only the runbook plan. Do not execute real-machine operations from this request.\n"
        "- Preserve previous findings and review documents as context.\n"
        "- Prefer scope reduction over asking a human to rewrite the plan. If a review point is too broad, split it into this-run scope and follow-up investigation scope.\n"
        "- Human review is the last resort. Use operator_review only for true policy decisions, unsafe operations, or irreducible ambiguity.\n"
    )


def _attach_revision_request(run: dict[str, Any], chief: dict[str, Any], *, revision_no: int) -> str:
    title = f"Runbook revision request {revision_no} for run {run.get('id')}"
    summary = (
        f"Chief verdict={chief.get('verdict')}; risk={chief.get('risk_verdict')}; "
        f"technical={chief.get('technical_verdict')}. Runbook plan requires revision before execution."
    )
    created = common.knowledge_attach_run_document(str(run["id"]), {
        "role": f"runbook_revision_request_{revision_no}",
        "ticket_id": run.get("ticket_id"),
        "kind": "runbook-revision-request",
        "title": title,
        "summary": summary,
        "body_md": _revision_request_body(chief, revision_no=revision_no),
        "tags": ["runbook-revision-request", "ai-generated", f"revision:{revision_no}"],
        "source": "zendesk-support-ai-runbook-review-worker",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    return str(document.get("id") or "")


def _next_status(chief: dict[str, Any], *, revision_no: int) -> tuple[str, str]:
    verdict = str(chief.get("verdict") or "")
    if verdict == "block":
        return "operator_review", "Runbook chief review blocked; human operator review required."
    if verdict == "revise":
        if revision_no > RUNBOOK_MAX_REVISIONS:
            return "operator_review", (
                f"Runbook revision limit reached: {revision_no}>{RUNBOOK_MAX_REVISIONS}. "
                "Human should decide whether to approve a scoped-down runbook or open a follow-up investigation."
            )
        return "revision_requested", "Runbook chief review requested revision before execution."
    return "review_passed", "Runbook chief review passed; ready for human execution review."


def process_one(run_ref: dict[str, Any], *, verbose: bool = False) -> bool:
    run_id = str(run_ref["id"])
    try:
        run = common.knowledge_get_run(run_id)
        if run.get("status") not in {"review_requested", "planned"}:
            if verbose:
                common.log(f"runbook review skip {run_id}: status={run.get('status')}")
            return False
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        if not _has_plan(documents):
            common.knowledge_update_run(run_id, {
                "status": "operator_review",
                "issue_on_run": "runbook review failed: no runbook-plan document attached",
            })
            return False

        common.knowledge_update_run(run_id, {"status": "risk_reviewing"})
        risk = llm_client.generate_runbook_risk_review(run, documents)
        risk_doc_id = _attach_review(run, risk, kind="risk")

        common.knowledge_update_run(run_id, {"status": "technical_reviewing"})
        refreshed_docs = common.knowledge_list_run_documents(run_id, include_body=True)
        technical = llm_client.generate_runbook_technical_review(run, refreshed_docs)
        tech_doc_id = _attach_review(run, technical, kind="technical")

        chief_docs = common.knowledge_list_run_documents(run_id, include_body=True)
        chief = llm_client.generate_runbook_chief_review(run, chief_docs, risk, technical)
        chief_doc_id = _attach_review(run, chief, kind="chief")

        revision_no = _revision_count(chief_docs) + 1
        status, summary = _next_status(chief, revision_no=revision_no)
        if status == "revision_requested":
            _attach_revision_request(run, chief, revision_no=revision_no)
        common.knowledge_update_run(run_id, {
            "status": status,
            "summary": summary,
            "issue_on_run": "" if status != "operator_review" else summary,
        })
        if verbose:
            common.log(
                f"runbook reviewed {run_id}: {status} "
                f"risk={risk_doc_id} technical={tech_doc_id} chief={chief_doc_id}"
            )
        return True
    except Exception as exc:  # noqa: BLE001
        common.log(f"runbook review worker failed {run_id}: {exc}")
        try:
            common.knowledge_update_run(run_id, {
                "status": "operator_review",
                "issue_on_run": f"runbook review worker failed: {exc}",
            })
        except Exception as update_exc:  # noqa: BLE001
            common.log(f"runbook review worker failed to mark {run_id}: {update_exc}")
        return False


def run_once(verbose: bool = False, limit: int = 5) -> int:
    if not RUNBOOK_REVIEW_WORKER_ENABLED:
        if verbose:
            common.log("runbook review worker disabled")
        return 0
    if not common.knowledge_enabled():
        if verbose:
            common.log("runbook review worker skipped: Knowledge API not configured")
        return 0
    runs = common.knowledge_list_runs(status="review_requested", limit=limit)
    remaining = max(0, limit - len(runs))
    if remaining:
        runs.extend(common.knowledge_list_runs(status="planned", limit=remaining))
    n = 0
    for run in runs:
        if process_one(run, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"runbook review worker done: {len(runs)} candidate runs, {n} reviewed")
    return n


def run_forever(verbose: bool = False, interval: int = 60, limit: int = 5) -> None:
    common.log(f"runbook review worker start (interval={interval}s limit={limit})")
    while True:
        try:
            run_once(verbose=verbose, limit=limit)
        except Exception as exc:  # noqa: BLE001
            common.log(f"runbook review worker loop error (continuing): {exc}")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Runbook review worker")
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
