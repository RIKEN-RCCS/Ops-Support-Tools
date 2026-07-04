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
RUNBOOK_REVIEW_MAX_TRANSIENT_RETRIES = int(os.environ.get("SUPPORT_AI_RUNBOOK_REVIEW_MAX_TRANSIENT_RETRIES", "10"))


def _md_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip()) or "- none"


def _revision_count(documents: list[dict[str, Any]]) -> int:
    return sum(1 for doc in documents if doc.get("kind") == "runbook-revision-request")


def _review_retry_count(documents: list[dict[str, Any]]) -> int:
    latest_plan_at = 0
    for doc in documents:
        if doc.get("kind") == "runbook-plan":
            latest_plan_at = max(latest_plan_at, int(doc.get("linked_at") or doc.get("created_at") or 0))
    return sum(
        1
        for doc in documents
        if doc.get("kind") == "runbook-review-retry"
        and int(doc.get("linked_at") or doc.get("created_at") or 0) >= latest_plan_at
    )


def _has_plan(documents: list[dict[str, Any]]) -> bool:
    return any(doc.get("kind") == "runbook-plan" for doc in documents)


def _latest_document(documents: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    latest_at = -1
    for doc in documents:
        if doc.get("kind") != kind:
            continue
        linked_at = int(doc.get("linked_at") or doc.get("created_at") or 0)
        if linked_at >= latest_at:
            latest = doc
            latest_at = linked_at
    return latest


def _deterministic_risk_review(run: dict[str, Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate execution safety from the run contract without an LLM dependency."""
    caps = {str(cap).strip() for cap in (run.get("required_capabilities") or []) if str(cap).strip()}
    executor_mode = str(run.get("executor_mode") or "")
    risk_level = str(run.get("risk_level") or "low")
    approval_required = bool(run.get("approval_required"))
    environment = str(run.get("environment") or "").strip()
    machine = str(run.get("machine") or "").strip()
    plan_body = str(_latest_document(documents, "runbook-plan").get("body_md") or "")
    plan_lower = plan_body.lower()

    unsafe_operations: list[str] = []
    missing_approvals: list[str] = []
    missing_risk_controls: list[str] = []
    revise_requests: list[str] = []
    stop_conditions: list[str] = []

    risky_caps = {"workspace_write", "compile", "job_submit", "user_data_access", "privileged"}
    high_risk_caps = {"user_data_access", "privileged"}
    present_risky_caps = sorted(caps & risky_caps)

    if not environment or not machine:
        missing_risk_controls.append("environment/machine is not fixed before real-machine execution.")
        revise_requests.append("Fix the target environment and machine before any real-machine action.")

    if present_risky_caps:
        unsafe_operations.append(
            "Runbook includes non-read-only capabilities that must stay inside the execution contract: "
            + ", ".join(present_risky_caps)
        )
        stop_conditions.append("Stop if the next required action exceeds required_capabilities or executor_mode.")

    if caps & high_risk_caps:
        unsafe_operations.append(
            "Runbook may touch privileged scope or user data; this needs explicit human/operator control."
        )
        if executor_mode != "human_with_ai":
            revise_requests.append("Change executor_mode to human_with_ai or remove privileged/user-data capabilities.")

    if present_risky_caps and executor_mode == "auto_agent_allowed":
        missing_risk_controls.append("auto_agent_allowed is too broad for compile/job_submit/write/user-data capabilities.")
        revise_requests.append("Use human_with_ai for risky capabilities, or split out a read_only auto-agent task.")

    if present_risky_caps and not approval_required:
        missing_approvals.append("Risky capabilities are present but approval_required is false.")
        revise_requests.append("Set approval_required=true and state who must approve before execution.")

    if risk_level in {"medium", "high", "blocked"} and not approval_required:
        missing_approvals.append(f"risk_level={risk_level} requires explicit approval before execution.")

    if present_risky_caps and "stop" not in plan_lower and "停止" not in plan_body:
        missing_risk_controls.append("No explicit stop condition was found in the latest runbook plan.")
        revise_requests.append("Add concrete stop conditions for errors, scope expansion, and missing approval.")

    if "read_only" not in caps and not present_risky_caps:
        missing_risk_controls.append("required_capabilities is empty or unclear.")
        revise_requests.append("Declare required_capabilities so an executor can decide whether it is allowed to claim.")

    if "privileged" in caps and executor_mode != "human_with_ai":
        verdict = "block"
    elif revise_requests or missing_approvals or missing_risk_controls:
        verdict = "revise"
    else:
        verdict = "pass"

    if verdict == "pass":
        summary = (
            "Risk contract is acceptable: target is fixed, risky capabilities are explicit, "
            "human approval/executor mode are consistent, and stop conditions are present."
        )
    else:
        summary = "Risk contract needs revision before execution."

    return {
        "verdict": verdict,
        "risk_level": risk_level if risk_level in {"low", "medium", "high", "blocked"} else "medium",
        "summary": summary,
        "requires_human_approval": approval_required or bool(present_risky_caps),
        "unsafe_operations": unsafe_operations,
        "missing_approvals": missing_approvals,
        "missing_risk_controls": missing_risk_controls,
        "revise_requests": revise_requests,
        "stop_conditions": stop_conditions,
        "operator_notes": (
            "Rule-based risk review. Technical adequacy, module choice, ABI compatibility, "
            "Knowledge search quality, and answer readiness are intentionally left to technical/chief review."
        ),
        "_model": "deterministic-risk-v1",
    }


def _has_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def _deterministic_technical_review(
    run: dict[str, Any],
    documents: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    """Fallback technical review when the LLM review path is transiently unavailable."""
    caps = {str(cap).strip() for cap in (run.get("required_capabilities") or []) if str(cap).strip()}
    plan_body = str(_latest_document(documents, "runbook-plan").get("body_md") or "")
    missing_knowledge_queries: list[str] = []
    known_issue_checks: list[str] = []
    unsupported_assumptions: list[str] = []
    revise_requests: list[str] = []

    required_sections = [
        "## Problem Summary",
        "## Environment Scope",
        "## Execution Steps",
        "## Findings Template",
        "## Summary Template",
    ]
    missing_sections = [section for section in required_sections if section not in plan_body]
    if missing_sections:
        revise_requests.append("Add missing runbook sections: " + ", ".join(missing_sections))

    if "read_only" in caps and not _has_any(plan_body, ["module", "version", "which", "show", "spider", "avail"]):
        revise_requests.append("Add concrete read-only commands or observations to collect before any risky action.")

    if "compile" in caps and not _has_any(plan_body, ["compile", "build", "gcc", "nvcc", "mpicc", "mpic++"]):
        revise_requests.append("Add the compile/build evidence to collect, including compiler wrapper and log capture.")

    if "job_submit" in caps and not _has_any(plan_body, ["job", "submit", "sbatch", "pjsub", "srun", "mpirun", "mpiexec"]):
        revise_requests.append("Add the job execution evidence to collect, or remove job_submit from required_capabilities.")

    if not _has_any(plan_body, ["findings", "issue on run", "issue_on_run", "summary", "answer draft"]):
        revise_requests.append("State how findings, issue_on_run, summary, and answer_draft should be registered.")

    if _has_any(plan_body, ["hpc-x", "hpcx", "mpi", "cuda", "gcc"]):
        known_issue_checks.append(
            "Record exact module/compiler/MPI/CUDA observations so future cases can judge freshness and reuse."
        )
    else:
        missing_knowledge_queries.append(
            "The task is about CUDA/GCC/MPI/HPC-X, but the plan does not visibly preserve those search terms."
        )

    if "自前ビルド" in plan_body or "build HPC-X" in plan_body:
        unsupported_assumptions.append(
            "Do not conclude that self-building HPC-X is recommended until policy/Knowledge and execution evidence support it."
        )

    verdict = "revise" if revise_requests else "pass"
    return {
        "verdict": verdict,
        "confidence": "medium" if verdict == "pass" else "low",
        "summary": (
            "Fallback technical review used because the LLM review call failed transiently. "
            "The review checks document structure, scoped evidence collection, and output handoff only."
        ),
        "missing_knowledge_queries": missing_knowledge_queries,
        "known_issue_checks": known_issue_checks,
        "unsupported_assumptions": unsupported_assumptions,
        "revise_requests": revise_requests,
        "answer_readiness": "ready_after_findings" if verdict == "pass" else "not_ready",
        "operator_notes": f"LLM technical review unavailable; fallback reason: {reason}",
        "_model": "deterministic-technical-fallback-v1",
    }


def _deterministic_chief_review(
    run: dict[str, Any],
    documents: list[dict[str, Any]],
    risk: dict[str, Any],
    technical: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    risk_verdict = str(risk.get("verdict") or "revise")
    technical_verdict = str(technical.get("verdict") or "revise")
    risk_requests = list(risk.get("revise_requests") or [])
    technical_requests = list(technical.get("revise_requests") or [])
    final_requests = risk_requests + technical_requests
    if risk_verdict == "block" or technical_verdict == "block":
        verdict = "block"
    elif final_requests or risk_verdict == "revise" or technical_verdict == "revise":
        verdict = "revise"
    else:
        verdict = "pass"

    evidence = []
    plan_body = str(_latest_document(documents, "runbook-plan").get("body_md") or "")
    if _has_any(plan_body, ["findings", "summary", "answer draft", "answer_draft"]):
        evidence.append("Register findings, issue_on_run, summary, and answer_draft after execution.")
    if run.get("required_capabilities"):
        evidence.append("Record which required_capabilities were actually used and which were skipped.")

    patch_instructions = [
        f"Update the latest runbook plan to address: {request}" for request in final_requests
    ]

    return {
        "verdict": verdict,
        "summary": (
            "Fallback chief review used because the LLM chief call failed transiently. "
            "Risk and technical findings were merged without adding new broad scope."
        ),
        "risk_verdict": risk_verdict if risk_verdict in {"pass", "revise", "block"} else "revise",
        "technical_verdict": technical_verdict if technical_verdict in {"pass", "revise", "block"} else "revise",
        "risk_points": list(risk.get("unsafe_operations") or []) + list(risk.get("missing_risk_controls") or []),
        "technical_points": (
            list(technical.get("missing_knowledge_queries") or [])
            + list(technical.get("known_issue_checks") or [])
            + list(technical.get("unsupported_assumptions") or [])
        ),
        "reviewer_conflicts": [],
        "missing_coverage": final_requests,
        "final_revise_requests": final_requests,
        "planner_patch_instructions": patch_instructions,
        "evidence_to_collect": evidence,
        "pass_conditions": [
            "All final_revise_requests are reflected in the latest runbook plan.",
            "Execution remains within required_capabilities, executor_mode, approval, and stop conditions.",
        ],
        "human_decision_needed": [],
        "operator_notes": f"LLM chief review unavailable; fallback reason: {reason}",
        "_model": "deterministic-chief-fallback-v1",
    }


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


def _is_transient_review_error(exc: Exception) -> bool:
    text = str(exc).lower()
    transient_markers = (
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "503",
        "502",
        "504",
    )
    return any(marker in text for marker in transient_markers)


def _attach_review_retry(run_id: str, run: dict[str, Any], exc: Exception, *, retry_no: int) -> str:
    title = f"Runbook review transient retry {retry_no} for run {run_id}"
    body = (
        "# Runbook Review Retry\n\n"
        f"- retry_no: {retry_no}\n"
        f"- max_transient_retries: {RUNBOOK_REVIEW_MAX_TRANSIENT_RETRIES}\n"
        "- reason: transient LLM/API failure during runbook review\n\n"
        "## Error\n"
        f"{str(exc)}\n\n"
        "## Handling\n"
        "The run is returned to `review_requested` so the review worker can retry automatically. "
        "This is not a human/operator review of the runbook content.\n"
    )
    created = common.knowledge_attach_run_document(run_id, {
        "role": f"runbook_review_retry_{retry_no}",
        "ticket_id": run.get("ticket_id"),
        "kind": "runbook-review-retry",
        "title": title,
        "summary": f"Transient review failure; retry {retry_no}/{RUNBOOK_REVIEW_MAX_TRANSIENT_RETRIES}.",
        "body_md": body,
        "tags": ["runbook-review-retry", "transient-error", f"retry:{retry_no}"],
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
        "- Human review is the last resort. Use human_review only for true policy decisions, unsafe operations, or irreducible ambiguity.\n"
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
        return "human_review", "Runbook chief review blocked; human operator review required."
    if verdict == "revise":
        if revision_no > RUNBOOK_MAX_REVISIONS:
            return "human_review", (
                f"Runbook revision limit reached: {revision_no}>{RUNBOOK_MAX_REVISIONS}. "
                "Human should decide whether to approve a scoped-down runbook or open a follow-up investigation."
            )
        return "revision_requested", "Runbook chief review requested revision before execution."
    return "review_passed", "Runbook chief review passed; ready for human execution review."


def process_one(run_ref: dict[str, Any], *, verbose: bool = False) -> bool:
    run_id = str(run_ref["id"])
    try:
        run = common.knowledge_get_run(run_id)
        if run.get("status") not in {"review_requested", "planned", "risk_reviewing"}:
            if verbose:
                common.log(f"runbook review skip {run_id}: status={run.get('status')}")
            return False
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        if not _has_plan(documents):
            common.knowledge_update_run(run_id, {
                "status": "human_review",
                "issue_on_run": "runbook review failed: no runbook-plan document attached",
            })
            return False

        common.knowledge_update_run(run_id, {"status": "risk_reviewing"})
        risk = _deterministic_risk_review(run, documents)
        risk_doc_id = _attach_review(run, risk, kind="risk")

        common.knowledge_update_run(run_id, {"status": "technical_reviewing"})
        refreshed_docs = common.knowledge_list_run_documents(run_id, include_body=True)
        try:
            technical = llm_client.generate_runbook_technical_review(run, refreshed_docs)
        except Exception as technical_exc:  # noqa: BLE001
            if not _is_transient_review_error(technical_exc):
                raise
            technical = _deterministic_technical_review(
                run,
                refreshed_docs,
                reason=str(technical_exc),
            )
        tech_doc_id = _attach_review(run, technical, kind="technical")

        chief_docs = common.knowledge_list_run_documents(run_id, include_body=True)
        try:
            chief = llm_client.generate_runbook_chief_review(run, chief_docs, risk, technical)
        except Exception as chief_exc:  # noqa: BLE001
            if not _is_transient_review_error(chief_exc):
                raise
            chief = _deterministic_chief_review(
                run,
                chief_docs,
                risk,
                technical,
                reason=str(chief_exc),
            )
        chief_doc_id = _attach_review(run, chief, kind="chief")

        revision_no = _revision_count(chief_docs) + 1
        status, summary = _next_status(chief, revision_no=revision_no)
        if status == "revision_requested":
            _attach_revision_request(run, chief, revision_no=revision_no)
        common.knowledge_update_run(run_id, {
            "status": status,
            "summary": summary,
            "issue_on_run": "" if status != "human_review" else summary,
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
            run = common.knowledge_get_run(run_id)
            documents = common.knowledge_list_run_documents(run_id, include_body=False)
            retry_no = _review_retry_count(documents) + 1
            if _is_transient_review_error(exc):
                _attach_review_retry(run_id, run, exc, retry_no=retry_no)
                if retry_no < RUNBOOK_REVIEW_MAX_TRANSIENT_RETRIES:
                    common.knowledge_update_run(run_id, {
                        "status": "review_requested",
                        "issue_on_run": (
                            f"runbook review transient failure; retry "
                            f"{retry_no}/{RUNBOOK_REVIEW_MAX_TRANSIENT_RETRIES}: {exc}"
                        ),
                    })
                else:
                    common.knowledge_update_run(run_id, {
                        "status": "human_review",
                        "issue_on_run": (
                            f"runbook review transient retry limit reached "
                            f"{retry_no}/{RUNBOOK_REVIEW_MAX_TRANSIENT_RETRIES}: {exc}"
                        ),
                    })
            else:
                common.knowledge_update_run(run_id, {
                    "status": "human_review",
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
    runs = []
    for _ in range(limit):
        run = common.knowledge_worker_claim_run(
            worker="runbook-review-worker",
            statuses=["review_requested", "planned"],
            claim_status="risk_reviewing",
            task_type="real_machine",
        )
        if not run:
            break
        runs.append(run)
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
