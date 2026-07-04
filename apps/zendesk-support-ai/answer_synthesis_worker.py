#!/usr/bin/env python3
"""Answer synthesis worker for executed runbooks.

This worker reads result_registered runs with execution-result documents and
creates a stronger answer_draft from findings, issues, summary, and the
existing draft. It never posts to Zendesk.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

import common
import llm_client


ANSWER_SYNTHESIS_WORKER_ENABLED = os.environ.get(
    "SUPPORT_AI_ANSWER_SYNTHESIS_WORKER_ENABLED", "1"
).lower() in ("1", "true", "yes")
ANSWER_EVALUATION_SCHEMA_VERSION = "answer-question-evaluation-v2"


def _md_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip()) or "- none"


def _latest_doc(documents: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    matches = [doc for doc in documents if doc.get("kind") == kind]
    if not matches:
        return None
    return matches[-1]


def _has_execution_result_package(documents: list[dict[str, Any]]) -> bool:
    kinds = {str(doc.get("kind") or "") for doc in documents}
    return {"findings", "summary", "answer_draft"}.issubset(kinds)


def _already_synthesized(documents: list[dict[str, Any]]) -> bool:
    latest_answer = _latest_doc(documents, "answer_draft")
    if not latest_answer:
        return False
    source = str(latest_answer.get("source") or "")
    role = str(latest_answer.get("role") or "")
    return source == "zendesk-support-ai-answer-synthesis-worker" or role == "answer_draft_synthesized"


def _already_evaluated(documents: list[dict[str, Any]]) -> bool:
    latest_eval = _latest_doc(documents, "answer-question-evaluation")
    latest_answer = _latest_doc(documents, "answer_draft")
    if not latest_eval or not latest_answer:
        return False
    if int(latest_eval.get("created_at") or 0) < int(latest_answer.get("created_at") or 0):
        return False
    body = str(latest_eval.get("body_md") or "")
    tags = {str(tag) for tag in latest_eval.get("tags") or []}
    return ANSWER_EVALUATION_SCHEMA_VERSION in tags and f"- schema: {ANSWER_EVALUATION_SCHEMA_VERSION}" in body


def _ticket_context(ticket_id: Any) -> dict[str, Any]:
    if ticket_id in (None, ""):
        return {"ticket_id": "", "subject": "", "comments": []}
    tid = int(ticket_id)
    ticket = common.fetch_ticket(tid)
    comments = common.fetch_ticket_comments(tid)
    public_comments = [
        {
            "created_at": comment.get("created_at"),
            "public": bool(comment.get("public")),
            "body": str(comment.get("plain_body") or comment.get("body") or "")[:6000],
        }
        for comment in comments
        if comment.get("public")
    ]
    return {
        "ticket_id": tid,
        "subject": str(ticket.get("subject") or ""),
        "description": str(ticket.get("description") or "")[:6000],
        "comments": public_comments[-8:],
    }


def _quality_review_body(result: dict[str, Any]) -> str:
    return (
        "# Answer Quality Review\n\n"
        f"- model: {result.get('_model', 'unknown')}\n"
        f"- quality: {result.get('quality')}\n"
        f"- confidence: {result.get('confidence')}\n"
        f"- safe_to_send: {'yes' if result.get('safe_to_send') else 'no'}\n"
        f"- followup_runbook_needed: {'yes' if result.get('followup_runbook_needed') else 'no'}\n\n"
        "## Review Notes\n"
        f"{_md_list(result.get('review_notes') or [])}\n\n"
        "## Missing Evidence\n"
        f"{_md_list(result.get('missing_evidence') or [])}\n\n"
        "## Followup Scope\n"
        f"{str(result.get('followup_scope') or '').strip() or 'none'}\n\n"
        "## Operator Notes\n"
        f"{str(result.get('operator_notes') or '').strip()}\n"
    )


def _question_evaluation_body(result: dict[str, Any]) -> str:
    return (
        "# Answer Question Evaluation\n\n"
        f"- schema: {ANSWER_EVALUATION_SCHEMA_VERSION}\n"
        f"- model: {result.get('_model', 'unknown')}\n"
        f"- verdict: {result.get('verdict')}\n"
        f"- confidence: {result.get('confidence')}\n"
        f"- recommended_operator_action: {result.get('recommended_operator_action')}\n\n"
        "## Question Summary\n"
        f"{str(result.get('question_summary') or '').strip()}\n\n"
        "## Answer Summary\n"
        f"{str(result.get('answer_summary') or '').strip()}\n\n"
        "## Covered Points\n"
        f"{_md_list(result.get('covered_points') or [])}\n\n"
        "## Unanswered Points\n"
        f"{_md_list(result.get('unanswered_points') or [])}\n\n"
        "## Unsupported Claims\n"
        f"{_md_list(result.get('unsupported_claims') or [])}\n\n"
        "## Overstatements\n"
        f"{_md_list(result.get('overstatements') or [])}\n\n"
        "## Real-Machine Investigable Points\n"
        f"{_md_list(result.get('real_machine_investigable_points') or [])}\n\n"
        "## Knowledge Research Points\n"
        f"{_md_list(result.get('knowledge_research_points') or [])}\n\n"
        "## Human Decision Points\n"
        f"{_md_list(result.get('human_decision_points') or [])}\n\n"
        "## Additional Investigation Task Scope\n"
        f"{str(result.get('additional_investigation_scope') or '').strip() or 'none'}\n\n"
        "## Revision Instructions\n"
        f"{_md_list(result.get('revision_instructions') or [])}\n\n"
        "## Operator Notes\n"
        f"{str(result.get('operator_notes') or '').strip()}\n"
    )


def _answer_body(result: dict[str, Any], *, run: dict[str, Any]) -> str:
    return (
        "# Answer Draft\n\n"
        f"- source_run_id: {run.get('id')}\n"
        f"- ticket_id: {run.get('ticket_id') or ''}\n"
        f"- environment: {run.get('environment') or ''}\n"
        f"- machine: {run.get('machine') or ''}\n"
        "- answer_draft_policy: hold\n"
        f"- synthesis_quality: {result.get('quality')}\n"
        f"- safe_to_send: {'yes' if result.get('safe_to_send') else 'no'}\n\n"
        f"{str(result.get('answer_draft') or '').strip()}\n"
    )


def _attach_synthesis(run: dict[str, Any], result: dict[str, Any]) -> tuple[str, str]:
    run_id = str(run["id"])
    ticket_id = run.get("ticket_id")
    tags = [
        "answer-synthesis",
        "ai-generated",
        f"quality:{result.get('quality')}",
        f"safe_to_send:{'yes' if result.get('safe_to_send') else 'no'}",
    ]
    review_created = common.knowledge_attach_run_document(run_id, {
        "role": "answer_quality_review",
        "ticket_id": ticket_id,
        "kind": "answer-quality-review",
        "title": f"Answer quality review for run {run_id}",
        "summary": str(result.get("operator_notes") or result.get("quality") or "Answer quality review"),
        "body_md": _quality_review_body(result),
        "tags": tags,
        "source": "zendesk-support-ai-answer-synthesis-worker",
    })
    answer_created = common.knowledge_attach_run_document(run_id, {
        "role": "answer_draft_synthesized",
        "ticket_id": ticket_id,
        "kind": "answer_draft",
        "title": f"Synthesized answer draft for run {run_id}",
        "summary": str(result.get("operator_notes") or "Synthesized answer draft"),
        "body_md": _answer_body(result, run=run),
        "tags": tags + ["answer_draft"],
        "source": "zendesk-support-ai-answer-synthesis-worker",
    })
    review_doc = review_created.get("document") if isinstance(review_created, dict) else {}
    answer_doc = answer_created.get("document") if isinstance(answer_created, dict) else {}
    return str(review_doc.get("id") or ""), str(answer_doc.get("id") or "")


def _attach_question_evaluation(run: dict[str, Any], result: dict[str, Any]) -> str:
    run_id = str(run["id"])
    created = common.knowledge_attach_run_document(run_id, {
        "role": "answer_question_evaluation",
        "ticket_id": run.get("ticket_id"),
        "kind": "answer-question-evaluation",
        "title": f"Answer question evaluation for run {run_id}",
        "summary": str(result.get("operator_notes") or result.get("verdict") or "Answer question evaluation"),
        "body_md": _question_evaluation_body(result),
        "tags": [
            "answer-question-evaluation",
            ANSWER_EVALUATION_SCHEMA_VERSION,
            "ai-generated",
            f"verdict:{result.get('verdict')}",
            f"action:{result.get('recommended_operator_action')}",
        ],
        "source": "zendesk-support-ai-answer-synthesis-worker",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    return str(document.get("id") or "")


def _evaluate_if_needed(run: dict[str, Any], documents: list[dict[str, Any]], *, verbose: bool = False) -> bool:
    if _already_evaluated(documents):
        if verbose:
            common.log(f"answer evaluation skip {run.get('id')}: current answer already evaluated")
        return False
    if not _latest_doc(documents, "answer_draft"):
        return False
    ticket_context = _ticket_context(run.get("ticket_id"))
    result = llm_client.evaluate_answer_against_question(ticket_context, run, documents)
    doc_id = _attach_question_evaluation(run, result)
    if verbose:
        common.log(f"answer evaluated {run.get('id')}: evaluation={doc_id}")
    return True


def process_one(run_ref: dict[str, Any], *, verbose: bool = False) -> bool:
    run_id = str(run_ref["id"])
    try:
        run = common.knowledge_get_run(run_id)
        if run.get("status") not in {"result_registered", "answer_synthesizing", "operator_review"}:
            return False
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        if not _has_execution_result_package(documents):
            if verbose:
                common.log(f"answer synthesis skip {run_id}: execution result package incomplete")
            return False
        common.knowledge_update_run(run_id, {"status": "answer_synthesizing"})
        did_work = False
        if _already_synthesized(documents):
            if verbose:
                common.log(f"answer synthesis skip {run_id}: latest answer already synthesized")
        else:
            result = llm_client.generate_runbook_answer_synthesis(run, documents)
            review_doc_id, answer_doc_id = _attach_synthesis(run, result)
            summary = (
                f"Answer synthesis generated by {result.get('_model', 'unknown')}; "
                f"quality={result.get('quality')}; safe_to_send={result.get('safe_to_send')}."
            )
            common.knowledge_update_run(run_id, {"summary": summary})
            did_work = True
            if verbose:
                common.log(f"answer synthesized {run_id}: review={review_doc_id} answer={answer_doc_id}")
            documents = common.knowledge_list_run_documents(run_id, include_body=True)
        did_work = _evaluate_if_needed(run, documents, verbose=verbose) or did_work
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        latest_eval = _latest_doc(documents, "answer-question-evaluation")
        next_status = "answer_review"
        if latest_eval:
            body = str(latest_eval.get("body_md") or "")
            if "recommended_operator_action: request_additional_investigation" in body:
                next_status = "routing_requested"
        common.knowledge_update_run(run_id, {"status": next_status})
        return did_work
    except Exception as exc:  # noqa: BLE001
        common.log(f"answer synthesis failed {run_id}: {exc}")
        return False


def run_once(verbose: bool = False, limit: int = 5) -> int:
    if not ANSWER_SYNTHESIS_WORKER_ENABLED:
        if verbose:
            common.log("answer synthesis worker disabled")
        return 0
    if not common.knowledge_enabled():
        if verbose:
            common.log("answer synthesis skipped: Knowledge API not configured")
        return 0
    runs = []
    for _ in range(limit):
        run = common.knowledge_worker_claim_run(
            worker="answer-synthesis-worker",
            statuses=["result_registered"],
            claim_status="answer_synthesizing",
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
        common.log(f"answer synthesis worker done: {len(runs)} candidate runs, {n} synthesized")
    return n


def run_forever(verbose: bool = False, interval: int = 60, limit: int = 5) -> None:
    common.log(f"answer synthesis worker start (interval={interval}s limit={limit})")
    while True:
        try:
            run_once(verbose=verbose, limit=limit)
        except Exception as exc:  # noqa: BLE001
            common.log(f"answer synthesis loop error (continuing): {exc}")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Runbook answer synthesis worker")
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
