#!/usr/bin/env python3
"""Split broad real-machine scopes into small executable investigation tasks."""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

import common
import llm_client


REAL_MACHINE_TASK_SPLITTER_WORKER_ENABLED = os.environ.get(
    "SUPPORT_AI_REAL_MACHINE_TASK_SPLITTER_WORKER_ENABLED", "1"
).lower() in ("1", "true", "yes")

SPLIT_SCHEMA_VERSION = "real-machine-task-split-v1"
CHILD_SCHEMA_VERSION = "real-machine-investigation-request-v1"


def _md_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip()) or "- none"


def _latest_doc(documents: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    matches = [doc for doc in documents if doc.get("kind") == kind]
    return matches[-1] if matches else None


def _already_split(documents: list[dict[str, Any]]) -> bool:
    return any(
        doc.get("kind") == "real-machine-task-split-plan"
        and SPLIT_SCHEMA_VERSION in str(doc.get("body_md") or "")
        for doc in documents
    )


def _parent_case_context(run: dict[str, Any]) -> str:
    parent_run_id = str(run.get("parent_run_id") or "")
    if not parent_run_id:
        return ""
    try:
        parent = common.knowledge_get_run(parent_run_id)
        parent_docs = common.knowledge_list_run_documents(parent_run_id, include_body=True)
        siblings = common.knowledge_list_runs(parent_run_id=parent_run_id, limit=80)
    except Exception as exc:  # noqa: BLE001
        return f"parent case context unavailable: {exc}"

    important_kinds = {
        "investigation-router-plan",
        "knowledge-research-request",
        "knowledge-research-result",
        "policy-decision-request",
        "real-machine-scope-request",
        "real-machine-investigation-request",
        "answer-question-evaluation",
    }
    parts = [
        "# Parent Investigation Case",
        f"- id: {parent.get('id')}",
        f"- status: {parent.get('status')}",
        f"- summary: {parent.get('summary')}",
        "",
        "## Sibling Tasks",
    ]
    for sibling in siblings:
        parts.append(
            f"- {sibling.get('id')} type={sibling.get('task_type') or 'case'} "
            f"status={sibling.get('status')} summary={sibling.get('summary')}"
        )
    parts.append("")
    parts.append("## Parent Documents")
    for doc in parent_docs[-16:]:
        if doc.get("kind") not in important_kinds:
            continue
        parts.append(
            f"### {doc.get('kind')} / {doc.get('title')}\n"
            f"summary: {doc.get('summary')}\n"
            f"{str(doc.get('body_md') or '')[:4000]}"
        )
    return "\n\n".join(parts)[:20000]


def _split_plan_body(result: dict[str, Any], *, run: dict[str, Any]) -> str:
    lines = [
        "# Real-Machine Task Split Plan",
        "",
        f"- schema: {SPLIT_SCHEMA_VERSION}",
        f"- model: {result.get('_model', 'unknown')}",
        f"- scope_run_id: {run.get('id')}",
        f"- parent_run_id: {run.get('parent_run_id') or ''}",
        f"- ticket_id: {run.get('ticket_id') or ''}",
        "",
        "## Summary",
        str(result.get("summary") or "").strip(),
        "",
        "## Granularity Notes",
        str(result.get("granularity_notes") or "").strip(),
        "",
        "## Tasks",
    ]
    for idx, task in enumerate(result.get("tasks") or [], start=1):
        lines.extend([
            "",
            f"### {idx}. {task.get('title')}",
            f"- priority: {task.get('priority')}",
            "",
            "#### Scope",
            str(task.get("scope") or "").strip(),
            "",
            "#### Rationale",
            str(task.get("rationale") or "").strip(),
            "",
            "#### Evidence Required",
            _md_list(task.get("evidence_required") or []),
            "",
            "#### Success Criteria",
            _md_list(task.get("success_criteria") or []),
            "",
            "#### Out Of Scope",
            _md_list(task.get("out_of_scope") or []),
            "",
            "#### Dependency Notes",
            str(task.get("dependency_notes") or "").strip() or "none",
        ])
    lines.extend([
        "",
        "## Operator Notes",
        str(result.get("operator_notes") or "").strip(),
    ])
    return "\n".join(lines).strip() + "\n"


def _child_body(*, scope_run: dict[str, Any], task: dict[str, Any], index: int) -> str:
    return (
        "# Real-Machine Investigation Request\n\n"
        f"- schema: {CHILD_SCHEMA_VERSION}\n"
        f"- source_scope_run_id: {scope_run.get('id')}\n"
        f"- parent_run_id: {scope_run.get('parent_run_id') or ''}\n"
        f"- ticket_id: {scope_run.get('ticket_id') or ''}\n"
        f"- split_task_index: {index}\n"
        "- split_policy: small_independent_task\n"
        "- execution_contract: unset_by_splitter\n\n"
        "## Scope\n"
        f"{str(task.get('scope') or '').strip()}\n\n"
        "## Rationale\n"
        f"{str(task.get('rationale') or '').strip()}\n\n"
        "## Evidence Required\n"
        f"{_md_list(task.get('evidence_required') or [])}\n\n"
        "## Success Criteria\n"
        f"{_md_list(task.get('success_criteria') or [])}\n\n"
        "## Out Of Scope\n"
        f"{_md_list(task.get('out_of_scope') or [])}\n\n"
        "## Dependency Notes\n"
        f"{str(task.get('dependency_notes') or '').strip() or 'none'}\n\n"
        "## Later Runbook Contract\n"
        "- This request is not itself a runbook-plan.\n"
        "- The runbook worker must write concrete steps and propose required_capabilities, executor_mode, risk_level, approval, and stop conditions.\n"
        "- The runbook review worker must evaluate that proposal before execution.\n"
    )


def _attach_split_plan(run: dict[str, Any], result: dict[str, Any]) -> str:
    body = _split_plan_body(result, run=run)
    created = common.knowledge_attach_run_document(str(run["id"]), {
        "role": "real_machine_task_split_plan",
        "ticket_id": run.get("ticket_id"),
        "kind": "real-machine-task-split-plan",
        "title": f"Real-machine task split plan for scope {run.get('id')}",
        "summary": str(result.get("summary") or "Real-machine task split plan"),
        "body_md": body,
        "tags": ["real-machine-task-split", SPLIT_SCHEMA_VERSION],
        "source": "zendesk-support-ai-real-machine-task-splitter-worker",
        "environment": run.get("environment") or "",
        "machine": run.get("machine") or "",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    doc_id = str(document.get("id") or "")
    parent_run_id = str(run.get("parent_run_id") or "")
    if parent_run_id:
        common.knowledge_attach_run_document(parent_run_id, {
            "role": "real_machine_task_split_plan",
            "ticket_id": run.get("ticket_id"),
            "kind": "real-machine-task-split-plan",
            "title": f"Real-machine task split plan for scope {run.get('id')}",
            "summary": str(result.get("summary") or "Real-machine task split plan"),
            "body_md": body,
            "tags": ["real-machine-task-split", SPLIT_SCHEMA_VERSION],
            "source": "zendesk-support-ai-real-machine-task-splitter-worker",
            "environment": run.get("environment") or "",
            "machine": run.get("machine") or "",
        })
    return doc_id


def _create_child_task(scope_run: dict[str, Any], task: dict[str, Any], *, index: int) -> str:
    parent_run_id = str(scope_run.get("parent_run_id") or "")
    if not parent_run_id:
        raise RuntimeError("real_machine_scope task has no parent_run_id")
    body = _child_body(scope_run=scope_run, task=task, index=index)
    created = common.knowledge_create_run({
        "parent_run_id": parent_run_id,
        "ticket_id": scope_run.get("ticket_id"),
        "task_type": "real_machine",
        "task_priority": str(task.get("priority") or scope_run.get("task_priority") or "normal"),
        "required_capabilities": [],
        "executor_mode": "",
        "risk_level": "",
        "approval_required": False,
        "environment": scope_run.get("environment") or "",
        "machine": scope_run.get("machine") or "",
        "status": "requested",
        "summary": str(task.get("title") or f"Real-machine investigation task {index}"),
        "runbook": body,
    })
    child = created.get("run") if isinstance(created, dict) else {}
    child_id = str(child.get("id") or "")
    if not child_id:
        raise RuntimeError("Knowledge API did not return real-machine child task id")
    common.knowledge_attach_run_document(child_id, {
        "role": "real_machine_investigation_source",
        "ticket_id": scope_run.get("ticket_id"),
        "kind": "real-machine-investigation-source",
        "title": str(task.get("title") or f"Real-machine investigation task {index}"),
        "summary": str(task.get("scope") or task.get("title") or ""),
        "body_md": body,
        "tags": ["real-machine-investigation", CHILD_SCHEMA_VERSION, f"source-scope:{scope_run.get('id')}"],
        "source": "zendesk-support-ai-real-machine-task-splitter-worker",
        "environment": scope_run.get("environment") or "",
        "machine": scope_run.get("machine") or "",
    })
    return child_id


def process_one(run_ref: dict[str, Any], *, verbose: bool = False) -> bool:
    run_id = str(run_ref["id"])
    try:
        run = common.knowledge_get_run(run_id)
        if run.get("status") not in {"split_requested", "splitting"}:
            return False
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        if _already_split(documents):
            common.knowledge_update_run(run_id, {"status": "task_done"})
            return False
        if not _latest_doc(documents, "real-machine-scope-source"):
            common.knowledge_update_run(run_id, {
                "status": "human_review",
                "issue_on_run": "real-machine scope source document is missing.",
            })
            return False
        common.knowledge_update_run(run_id, {"status": "splitting"})
        result = llm_client.split_real_machine_tasks(
            run,
            documents,
            parent_case_context=_parent_case_context(run),
        )
        tasks = result.get("tasks") or []
        if not tasks:
            common.knowledge_update_run(run_id, {
                "status": "human_review",
                "issue_on_run": "real-machine splitter produced no child tasks.",
            })
            return False
        plan_id = _attach_split_plan(run, result)
        child_ids = []
        for idx, task in enumerate(tasks[:8], start=1):
            child_ids.append(_create_child_task(run, task, index=idx))
        common.knowledge_update_run(run_id, {
            "status": "task_done",
            "summary": f"Real-machine scope split into {len(child_ids)} task(s); split_plan={plan_id}",
            "issue_on_run": "",
        })
        if verbose:
            common.log(f"real-machine scope split {run_id}: children={', '.join(child_ids)}")
        return True
    except Exception as exc:  # noqa: BLE001
        common.log(f"real-machine task splitter failed {run_id}: {exc}")
        try:
            common.knowledge_update_run(run_id, {
                "status": "human_review",
                "issue_on_run": f"real-machine task splitter failed: {exc}",
            })
        except Exception as update_exc:  # noqa: BLE001
            common.log(f"real-machine task splitter failed to mark {run_id}: {update_exc}")
        return False


def run_once(verbose: bool = False, limit: int = 5) -> int:
    if not REAL_MACHINE_TASK_SPLITTER_WORKER_ENABLED:
        if verbose:
            common.log("real-machine task splitter worker disabled")
        return 0
    if not common.knowledge_enabled():
        if verbose:
            common.log("real-machine task splitter skipped: Knowledge API not configured")
        return 0
    runs = []
    for _ in range(limit):
        run = common.knowledge_worker_claim_run(
            worker="real-machine-task-splitter-worker",
            statuses=["split_requested"],
            claim_status="splitting",
            task_type="real_machine_scope",
        )
        if not run:
            break
        runs.append(run)
    n = 0
    for run in runs:
        if process_one(run, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"real-machine task splitter done: {len(runs)} candidate runs, {n} split")
    return n


def run_forever(verbose: bool = False, interval: int = 60, limit: int = 5) -> None:
    common.log(f"real-machine task splitter worker start (interval={interval}s limit={limit})")
    while True:
        try:
            run_once(verbose=verbose, limit=limit)
        except Exception as exc:  # noqa: BLE001
            common.log(f"real-machine task splitter loop error (continuing): {exc}")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-machine task splitter worker")
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
