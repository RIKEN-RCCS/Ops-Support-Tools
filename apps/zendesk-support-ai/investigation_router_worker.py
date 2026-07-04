#!/usr/bin/env python3
"""Route answer-evaluation gaps into typed investigation requests.

The router is DB-first: it creates Knowledge research requests before opening
real-machine or policy-decision work. Real-machine requests describe what must
be learned; runbook planning/review decides execution capabilities and risk.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

import common
import llm_client


INVESTIGATION_ROUTER_WORKER_ENABLED = os.environ.get(
    "SUPPORT_AI_INVESTIGATION_ROUTER_WORKER_ENABLED", "1"
).lower() in ("1", "true", "yes")

ROUTING_SCHEMA_VERSION = "investigation-router-plan-v1"
PRESEARCH_SCHEMA_VERSION = "investigation-knowledge-presearch-v1"
REAL_MACHINE_SCOPE_SCHEMA_VERSION = "real-machine-scope-request-v1"
KNOWLEDGE_RESEARCH_SCHEMA_VERSION = "knowledge-research-request-v2"
POLICY_DECISION_SCHEMA_VERSION = "policy-decision-request-v1"


def _md_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip()) or "- none"


def _latest_doc(documents: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    matches = [doc for doc in documents if doc.get("kind") == kind]
    return matches[-1] if matches else None


def _has_final_router_plan(documents: list[dict[str, Any]]) -> bool:
    return any(
        doc.get("kind") == "investigation-router-plan"
        and doc.get("source") == "zendesk-support-ai-investigation-router-worker"
        and "- routing_phase: final_split" in str(doc.get("body_md") or "")
        for doc in documents
    )


def _has_presearch_request(documents: list[dict[str, Any]]) -> bool:
    return any(
        doc.get("kind") == "knowledge-research-request"
        and PRESEARCH_SCHEMA_VERSION in str(doc.get("body_md") or "")
        for doc in documents
    )


def _has_knowledge_result(documents: list[dict[str, Any]]) -> bool:
    return any(doc.get("kind") == "knowledge-research-result" for doc in documents)


def _documents_with_child_results(run_id: str, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = list(documents)
    try:
        children = common.knowledge_list_runs(parent_run_id=run_id, limit=100)
    except Exception as exc:  # noqa: BLE001
        common.log(f"investigation router child listing failed {run_id}: {exc}")
        return enriched
    for child in children:
        if child.get("task_type") not in {"knowledge_research", "real_machine_scope", "real_machine", "policy_decision"}:
            continue
        try:
            child_docs = common.knowledge_list_run_documents(str(child["id"]), include_body=True)
        except Exception as exc:  # noqa: BLE001
            common.log(f"investigation router child document listing failed {child.get('id')}: {exc}")
            continue
        for doc in child_docs:
            doc = dict(doc)
            doc.setdefault("child_run_id", child.get("id"))
            doc.setdefault("child_task_type", child.get("task_type"))
            doc.setdefault("child_status", child.get("status"))
            enriched.append(doc)
    return enriched


def _request_tags(request: dict[str, Any]) -> list[str]:
    tags = [
        "investigation-request",
        f"type:{request.get('request_type')}",
    ]
    return tags


def _normalize_request(request: dict[str, Any]) -> dict[str, Any]:
    guarded = dict(request)
    request_type = str(guarded.get("request_type") or "")
    if request_type == "knowledge_research":
        pass
    elif request_type == "policy_decision":
        pass
    elif request_type == "real_machine":
        pass
    for key in ("required_capabilities", "executor_mode", "risk_level", "approval_required"):
        guarded.pop(key, None)
    return guarded


def _request_common_body(request: dict[str, Any]) -> str:
    return (
        f"- request_type: {request.get('request_type')}\n"
        f"- priority: {request.get('priority')}\n"
        f"- environment: {request.get('environment')}\n"
        f"- machine: {request.get('machine')}\n"
        "- execution_contract: unset_by_router\n\n"
        "## Scope\n"
        f"{str(request.get('scope') or '').strip()}\n\n"
        "## Rationale\n"
        f"{str(request.get('rationale') or '').strip()}\n\n"
        "## Freshness Requirement\n"
        f"{str(request.get('freshness_requirement') or '').strip()}\n\n"
        "## Evidence Required\n"
        f"{_md_list(request.get('evidence_required') or [])}\n\n"
        "## Staleness Risks\n"
        f"{_md_list(request.get('staleness_risks') or [])}\n\n"
        "## Success Criteria\n"
        f"{_md_list(request.get('success_criteria') or [])}\n"
    )


def _router_plan_body(result: dict[str, Any], *, run: dict[str, Any]) -> str:
    lines = [
        "# Investigation Router Plan",
        "",
        f"- schema: {ROUTING_SCHEMA_VERSION}",
        "- routing_phase: final_split",
        f"- model: {result.get('_model', 'unknown')}",
        f"- parent_run_id: {run.get('id')}",
        f"- ticket_id: {run.get('ticket_id') or ''}",
        "- routing_policy: db_first_with_freshness_check",
        "",
        "## Summary",
        str(result.get("summary") or "").strip(),
        "",
        "## DB-First Notes",
        str(result.get("db_first_notes") or "").strip(),
        "",
        "## Requests",
    ]
    for idx, request in enumerate(result.get("requests") or [], start=1):
        lines.extend([
            "",
            f"### {idx}. {request.get('title')}",
            _request_common_body(request).strip(),
        ])
    lines.extend([
        "",
        "## Operator Notes",
        str(result.get("operator_notes") or "").strip(),
    ])
    return "\n".join(lines).strip() + "\n"


def _attach_router_plan(run: dict[str, Any], result: dict[str, Any]) -> str:
    created = common.knowledge_attach_run_document(str(run["id"]), {
        "role": "investigation_router_plan",
        "ticket_id": run.get("ticket_id"),
        "kind": "investigation-router-plan",
        "title": f"Investigation router plan for run {run.get('id')}",
        "summary": str(result.get("summary") or "Investigation router plan"),
        "body_md": _router_plan_body(result, run=run),
        "tags": ["investigation-router", "db-first", ROUTING_SCHEMA_VERSION],
        "source": "zendesk-support-ai-investigation-router-worker",
        "environment": run.get("environment") or "",
        "machine": run.get("machine") or "",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    return str(document.get("id") or "")


def _presearch_request(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_type": "knowledge_research",
        "title": f"Initial DB/Knowledge presearch for case {run.get('id')}",
        "scope": (
            "Search existing Knowledge DB, RAG, web search, past findings, known issues, "
            "operation documents, and policy records before deciding whether real-machine "
            "or policy-decision tasks are needed."
        ),
        "rationale": (
            "The investigation router must see reusable or stale knowledge first, so it does not "
            "repeat the same real-machine work unnecessarily."
        ),
        "environment": run.get("environment") or "",
        "machine": run.get("machine") or "",
        "priority": str(run.get("task_priority") or "normal"),
        "freshness_requirement": "Use current records when available; treat old or low-context records as stale candidates.",
        "evidence_required": [
            "Reusable findings or documented policies relevant to this case.",
            "Stale, historical, or low-context records that require fresh check.",
            "Missing evidence that may require real-machine or policy-decision tasks.",
        ],
        "staleness_risks": [
            "Software stack, module, driver, compiler, MPI, or operational policy may have changed.",
            "Past observations may lack machine, environment, version, command, or date context.",
        ],
        "success_criteria": [
            "The router can decide next tasks using DB/Knowledge evidence instead of starting from scratch.",
        ],
    }


def _request_body(run: dict[str, Any], request: dict[str, Any], *, schema: str, child_run_id: str = "") -> str:
    child_line = f"- child_run_id: {child_run_id}\n" if child_run_id else ""
    return (
        f"# {request.get('title')}\n\n"
        f"- schema: {schema}\n"
        f"- parent_run_id: {run.get('id')}\n"
        f"{child_line}"
        f"- ticket_id: {run.get('ticket_id') or ''}\n"
        "- routing_policy: db_first_with_freshness_check\n\n"
        f"{_request_common_body(request)}"
    )


def _attach_request_document(run_id: str, run: dict[str, Any], request: dict[str, Any], *, kind: str, schema: str, child_run_id: str, summary: str) -> str:
    created = common.knowledge_attach_run_document(run_id, {
        "role": kind.replace("-", "_"),
        "ticket_id": run.get("ticket_id"),
        "kind": kind,
        "title": str(request.get("title") or kind),
        "summary": summary,
        "body_md": _request_body(run, request, schema=schema, child_run_id=child_run_id),
        "tags": _request_tags(request) + [schema],
        "source": "zendesk-support-ai-investigation-router-worker",
        "environment": request.get("environment") or run.get("environment") or "",
        "machine": request.get("machine") or run.get("machine") or "",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    return str(document.get("id") or "")


def _create_knowledge_research_run(run: dict[str, Any], request: dict[str, Any]) -> str:
    created = common.knowledge_create_run({
        "parent_run_id": run.get("id"),
        "ticket_id": run.get("ticket_id"),
        "task_type": "knowledge_research",
        "task_priority": str(request.get("priority") or ""),
        "required_capabilities": ["read_only"],
        "executor_mode": "auto_agent_allowed",
        "risk_level": "low",
        "approval_required": False,
        "environment": request.get("environment") or run.get("environment") or "",
        "machine": request.get("machine") or run.get("machine") or "",
        "status": "investigation_waiting",
        "summary": str(request.get("title") or "Knowledge research requested."),
        "runbook": "",
    })
    task = created.get("run") if isinstance(created, dict) else {}
    task_id = str(task.get("id") or "")
    if not task_id:
        raise RuntimeError("Knowledge API did not return knowledge research task run id")
    _attach_request_document(
        task_id,
        run,
        request,
        kind="knowledge-research-request",
        schema=KNOWLEDGE_RESEARCH_SCHEMA_VERSION,
        child_run_id=task_id,
        summary=str(request.get("scope") or request.get("title") or ""),
    )
    _attach_request_document(
        str(run["id"]),
        run,
        request,
        kind="knowledge-research-request",
        schema=KNOWLEDGE_RESEARCH_SCHEMA_VERSION,
        child_run_id=task_id,
        summary=f"Created knowledge research task run {task_id}",
    )
    return task_id


def _create_policy_decision_run(run: dict[str, Any], request: dict[str, Any]) -> str:
    created = common.knowledge_create_run({
        "parent_run_id": run.get("id"),
        "ticket_id": run.get("ticket_id"),
        "task_type": "policy_decision",
        "task_priority": str(request.get("priority") or ""),
        "required_capabilities": [],
        "executor_mode": "human_only",
        "risk_level": "medium",
        "approval_required": True,
        "environment": request.get("environment") or run.get("environment") or "",
        "machine": request.get("machine") or run.get("machine") or "",
        "status": "policy_review",
        "summary": str(request.get("title") or "Policy decision requested."),
        "runbook": "",
    })
    task = created.get("run") if isinstance(created, dict) else {}
    task_id = str(task.get("id") or "")
    if not task_id:
        raise RuntimeError("Knowledge API did not return policy decision task run id")
    _attach_request_document(
        task_id,
        run,
        request,
        kind="policy-decision-request",
        schema=POLICY_DECISION_SCHEMA_VERSION,
        child_run_id=task_id,
        summary=str(request.get("scope") or request.get("title") or ""),
    )
    _attach_request_document(
        str(run["id"]),
        run,
        request,
        kind="policy-decision-request",
        schema=POLICY_DECISION_SCHEMA_VERSION,
        child_run_id=task_id,
        summary=f"Created policy decision task run {task_id}",
    )
    return task_id


def _real_machine_runbook_body(run: dict[str, Any], request: dict[str, Any]) -> str:
    return (
        "# Real-Machine Investigation Scope\n\n"
        f"- schema: {REAL_MACHINE_SCOPE_SCHEMA_VERSION}\n"
        f"- parent_run_id: {run.get('id')}\n"
        f"- ticket_id: {run.get('ticket_id') or ''}\n"
        "- routing_policy: db_first_with_freshness_check\n\n"
        f"{_request_common_body(request)}\n\n"
        "## Splitter Contract\n"
        "- This is not an executable runbook.\n"
        "- Split this scope into small real_machine investigation tasks before runbook planning.\n"
        "- Each child task should be independently claimable and should produce one findings/summary/answer_draft package.\n"
        "- Do not decide required_capabilities, executor_mode, risk_level, or approval here.\n\n"
        "## Later Runbook Contract\n"
        "- unset_by_router: yes\n"
        "- The router only describes what must be learned on the real machine at scope level.\n"
        "- The runbook worker must propose required_capabilities, executor_mode, risk_level, approval, and stop conditions after it writes concrete steps.\n"
        "- The runbook review worker must evaluate that proposal before execution.\n\n"
        "## Required Output\n"
        "- findings: observed facts with commands, timestamps, environment, machine, versions, and conditions.\n"
        "- issue_on_run: blocked actions, missing inputs, exceeded proposed capabilities after review, or stale/insufficient DB evidence.\n"
        "- summary: what this real-machine investigation proves and what remains unresolved.\n"
        "- answer_draft: only if the scoped investigation provides enough evidence; otherwise mark hold.\n"
    )


def _create_real_machine_scope_run(run: dict[str, Any], request: dict[str, Any], *, wait_for_knowledge: bool = False) -> str:
    body = _real_machine_runbook_body(run, request)
    created = common.knowledge_create_run({
        "parent_run_id": run.get("id"),
        "ticket_id": run.get("ticket_id"),
        "task_type": "real_machine_scope",
        "task_priority": str(request.get("priority") or ""),
        "required_capabilities": [],
        "executor_mode": "",
        "risk_level": "",
        "approval_required": False,
        "environment": request.get("environment") or run.get("environment") or "",
        "machine": request.get("machine") or run.get("machine") or "",
        "status": "investigation_waiting" if wait_for_knowledge else "split_requested",
        "summary": str(request.get("title") or "Real-machine investigation scope requested."),
        "runbook": body,
    })
    child = created.get("run") if isinstance(created, dict) else {}
    child_id = str(child.get("id") or "")
    if not child_id:
        raise RuntimeError("Knowledge API did not return real-machine task run id")
    common.knowledge_attach_run_document(child_id, {
        "role": "real_machine_investigation_source",
        "ticket_id": run.get("ticket_id"),
        "kind": "real-machine-scope-source",
        "title": str(request.get("title") or "Real-machine investigation scope source"),
        "summary": str(request.get("scope") or request.get("title") or ""),
        "body_md": body,
        "tags": _request_tags(request) + [REAL_MACHINE_SCOPE_SCHEMA_VERSION],
        "source": "zendesk-support-ai-investigation-router-worker",
        "environment": request.get("environment") or run.get("environment") or "",
        "machine": request.get("machine") or run.get("machine") or "",
    })
    common.knowledge_attach_run_document(str(run["id"]), {
        "role": "real_machine_investigation_request",
        "ticket_id": run.get("ticket_id"),
        "kind": "real-machine-scope-request",
        "title": str(request.get("title") or "Real-machine investigation scope request"),
        "summary": f"Created real-machine scope run {child_id}",
        "body_md": (
            f"# Real-Machine Investigation Scope Request\n\n"
            f"- schema: {REAL_MACHINE_SCOPE_SCHEMA_VERSION}\n"
            f"- child_run_id: {child_id}\n\n"
            f"- waits_for_knowledge_research: {'yes' if wait_for_knowledge else 'no'}\n\n"
            f"{_request_common_body(request)}"
        ),
        "tags": _request_tags(request) + [REAL_MACHINE_SCOPE_SCHEMA_VERSION],
        "source": "zendesk-support-ai-investigation-router-worker",
        "environment": request.get("environment") or run.get("environment") or "",
        "machine": request.get("machine") or run.get("machine") or "",
    })
    return child_id


def _materialize_requests(run: dict[str, Any], result: dict[str, Any], *, verbose: bool = False) -> list[str]:
    created: list[str] = []
    requests = result.get("requests") or []
    has_knowledge = any(str(request.get("request_type") or "") == "knowledge_research" for request in requests)
    for request in requests:
        request = _normalize_request(request)
        request_type = str(request.get("request_type") or "")
        if request_type == "knowledge_research":
            task_id = _create_knowledge_research_run(run, request)
            created.append(f"knowledge:{task_id}")
        elif request_type == "policy_decision":
            task_id = _create_policy_decision_run(run, request)
            created.append(f"policy:{task_id}")
        elif request_type == "real_machine":
            child_id = _create_real_machine_scope_run(run, request, wait_for_knowledge=has_knowledge)
            created.append(f"real_machine_scope:{child_id}")
    if verbose:
        common.log(f"investigation router materialized {run.get('id')}: {', '.join(created) or 'none'}")
    return created


def process_one(run_ref: dict[str, Any], *, verbose: bool = False) -> bool:
    run_id = str(run_ref["id"])
    try:
        run = common.knowledge_get_run(run_id)
        if run.get("status") not in {"routing_requested", "routing", "operator_review"}:
            return False
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        routing_documents = _documents_with_child_results(run_id, documents)
        if _has_final_router_plan(documents):
            if verbose:
                common.log(f"investigation routing skip {run_id}: already routed")
            if run.get("status") in {"routing_requested", "routing"}:
                common.knowledge_update_run(run_id, {"status": "investigation_waiting"})
            return False
        common.knowledge_update_run(run_id, {"status": "routing"})
        if not _has_knowledge_result(routing_documents):
            if not _has_presearch_request(routing_documents):
                request = _presearch_request(run)
                task_id = _create_knowledge_research_run(run, request)
                if verbose:
                    common.log(f"investigation presearch requested {run_id}: knowledge={task_id}")
            common.knowledge_update_run(run_id, {
                "status": "investigation_waiting",
                "summary": "Waiting for initial DB/Knowledge presearch before final routing.",
            })
            return True
        result = llm_client.route_investigation_requests(run, routing_documents)
        plan_id = _attach_router_plan(run, result)
        created = _materialize_requests(run, result, verbose=verbose)
        request_types = {
            str(request.get("request_type") or "")
            for request in result.get("requests") or []
        }
        if "real_machine" in request_types:
            next_status = "investigation_waiting"
        elif "policy_decision" in request_types:
            next_status = "policy_review"
        elif created:
            next_status = "investigation_waiting"
        else:
            next_status = "human_review"
        common.knowledge_update_run(run_id, {
            "status": next_status,
            "summary": f"Investigation routed: {len(created)} request(s); router_plan={plan_id}",
        })
        if verbose:
            common.log(f"investigation routed {run_id}: plan={plan_id} requests={len(created)}")
        return True
    except Exception as exc:  # noqa: BLE001
        common.log(f"investigation router failed {run_id}: {exc}")
        return False


def run_once(verbose: bool = False, limit: int = 5) -> int:
    if not INVESTIGATION_ROUTER_WORKER_ENABLED:
        if verbose:
            common.log("investigation router worker disabled")
        return 0
    if not common.knowledge_enabled():
        if verbose:
            common.log("investigation router skipped: Knowledge API not configured")
        return 0
    runs = []
    for _ in range(limit):
        run = common.knowledge_worker_claim_run(
            worker="investigation-router-worker",
            statuses=["routing_requested"],
            claim_status="routing",
            task_type="investigation_case",
        )
        if not run:
            break
        runs.append(run)
    n = 0
    for run in runs:
        if process_one(run, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"investigation router worker done: {len(runs)} candidate runs, {n} routed")
    return n


def run_forever(verbose: bool = False, interval: int = 60, limit: int = 5) -> None:
    common.log(f"investigation router worker start (interval={interval}s limit={limit})")
    while True:
        try:
            run_once(verbose=verbose, limit=limit)
        except Exception as exc:  # noqa: BLE001
            common.log(f"investigation router loop error (continuing): {exc}")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Investigation router worker")
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
