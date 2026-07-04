#!/usr/bin/env python3
"""DB-first knowledge research worker.

This worker reads `knowledge-research-request` documents, searches the local
Knowledge API first, optionally queries configured RAG and web-search services,
and attaches a `knowledge-research-result` document. It does not post to
Zendesk and does not execute machine commands.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from typing import Any

import requests

import common
from secret_config import env_secret


KNOWLEDGE_RESEARCH_WORKER_ENABLED = os.environ.get(
    "SUPPORT_AI_KNOWLEDGE_RESEARCH_WORKER_ENABLED", "1"
).lower() in ("1", "true", "yes")
KNOWLEDGE_RESEARCH_SCHEMA_VERSION = "knowledge-research-result-v1"
KNOWLEDGE_RESEARCH_SOURCE_VERSION = "knowledge-research-request-v2"
PRESEARCH_SCHEMA_VERSION = "investigation-knowledge-presearch-v1"
MAX_QUERIES = int(os.environ.get("SUPPORT_AI_KNOWLEDGE_RESEARCH_MAX_QUERIES", "8"))
MAX_DOCUMENTS = int(os.environ.get("SUPPORT_AI_KNOWLEDGE_RESEARCH_MAX_DOCUMENTS", "10"))
RAG_SEARCH_URL = os.environ.get("SUPPORT_AI_RAG_SEARCH_URL", "").strip()
WEB_SEARCH_URL = os.environ.get("SUPPORT_AI_WEB_SEARCH_URL", "").strip()
RAG_API_KEY = env_secret("SUPPORT_AI_RAG_API_KEY")
WEB_SEARCH_API_KEY = env_secret("SUPPORT_AI_WEB_SEARCH_API_KEY")

TASK_DONE_STATUSES = {"task_done", "closed", "done"}


def _md_list(values: list[Any]) -> str:
    if not values:
        return "- none"
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip()) or "- none"


def _latest_docs_by_kind(documents: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in documents if doc.get("kind") == kind]


def _result_exists(documents: list[dict[str, Any]], request_doc_id: str) -> bool:
    needle = f"- request_document_id: {request_doc_id}"
    for doc in documents:
        if doc.get("kind") != "knowledge-research-result":
            continue
        if needle in str(doc.get("body_md") or ""):
            return True
    return False


def _body_without_meta(body: str, *, limit: int = 2400) -> str:
    lines = []
    for line in body.splitlines():
        if line.startswith("- ") and ":" in line and not lines:
            continue
        lines.append(line)
    return "\n".join(lines).strip()[:limit]


def _terms_from_text(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+./:-]{2,}|[一-龯ぁ-んァ-ンー]{2,}", text)
    stop = {
        "schema",
        "ticket",
        "source",
        "priority",
        "environment",
        "machine",
        "request",
        "routing",
        "policy",
        "evidence",
        "required",
        "success",
        "criteria",
        "none",
    }
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        key = token.lower()
        if key in stop or key in seen:
            continue
        seen.add(key)
        result.append(token)
        if len(result) >= 12:
            break
    return result


def _build_queries(run: dict[str, Any], request_doc: dict[str, Any]) -> list[str]:
    body = str(request_doc.get("body_md") or "")
    seeds = [
        str(run.get("machine") or "").strip(),
        str(run.get("environment") or "").strip(),
        str(request_doc.get("title") or "").strip(),
        str(request_doc.get("summary") or "").strip(),
        "findings",
        "documented_policy",
        "runbook-plan",
    ]
    seeds.extend(_terms_from_text(body))
    queries: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        seed = seed.strip()
        if not seed:
            continue
        key = seed.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(seed)
        if len(queries) >= MAX_QUERIES:
            break
    return queries


def _search_knowledge(queries: list[str], *, exclude_ids: set[str]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            for doc in common.knowledge_search_documents(query, limit=MAX_DOCUMENTS):
                doc_id = str(doc.get("id") or "")
                if not doc_id or doc_id in exclude_ids or doc_id in by_id:
                    continue
                by_id[doc_id] = dict(doc, matched_query=query)
        except Exception as exc:  # noqa: BLE001
            common.log(f"knowledge research search failed query={query!r}: {exc}")
    enriched: list[dict[str, Any]] = []
    for doc_id, doc in list(by_id.items())[:MAX_DOCUMENTS]:
        try:
            full = common.knowledge_get_document(doc_id)
            full["matched_query"] = doc.get("matched_query")
            enriched.append(full)
        except Exception as exc:  # noqa: BLE001
            common.log(f"knowledge research document fetch failed {doc_id}: {exc}")
            enriched.append(doc)
    return enriched


def _headers_for_optional_api(api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return headers


def _optional_search(name: str, url: str, api_key: str, query: str) -> dict[str, Any]:
    if not url:
        return {"source": name, "status": "not_configured", "results": []}
    try:
        resp = requests.get(
            url,
            params={"q": query, "limit": 5},
            headers=_headers_for_optional_api(api_key),
            timeout=common.HTTP_TIMEOUT,
        )
        if not resp.ok:
            return {"source": name, "status": f"error:{resp.status_code}", "results": [], "error": resp.text[:300]}
        data = resp.json()
        results = data.get("results") if isinstance(data, dict) else data
        if not isinstance(results, list):
            results = [data]
        return {"source": name, "status": "ok", "results": results[:5]}
    except Exception as exc:  # noqa: BLE001
        return {"source": name, "status": "error", "results": [], "error": str(exc)[:300]}


def _classify_document(doc: dict[str, Any]) -> dict[str, Any]:
    body = str(doc.get("body_md") or "")
    lower = body.lower()
    reproducibility = ""
    for line in body.splitlines():
        if line.lower().startswith("- reproducibility:"):
            reproducibility = line.split(":", 1)[1].strip()
            break
    stale_signals = []
    for marker in ("stale_after", "staleness_triggers", "historical", "unknown"):
        if marker in lower:
            stale_signals.append(marker)
    strong = str(doc.get("kind") or "") in {"findings", "summary"} and reproducibility in {
        "reproduced",
        "documented_policy",
    }
    reusable = strong or str(doc.get("kind") or "") in {"findings", "knowledge-research-result"}
    return {
        "id": doc.get("id"),
        "kind": doc.get("kind"),
        "title": doc.get("title"),
        "source": doc.get("source"),
        "environment": doc.get("environment"),
        "machine": doc.get("machine"),
        "matched_query": doc.get("matched_query"),
        "reproducibility": reproducibility or "unknown",
        "reusable": reusable,
        "stale_signals": stale_signals,
        "excerpt": _body_without_meta(body, limit=900),
    }


def _research_body(
    *,
    run: dict[str, Any],
    request_doc: dict[str, Any],
    queries: list[str],
    knowledge_docs: list[dict[str, Any]],
    rag_result: dict[str, Any],
    web_result: dict[str, Any],
) -> str:
    classified = [_classify_document(doc) for doc in knowledge_docs]
    reusable = [doc for doc in classified if doc["reusable"] and not doc["stale_signals"]]
    stale_or_weak = [doc for doc in classified if doc["stale_signals"] or doc["reproducibility"] in {"unknown", "historical"}]
    missing = []
    if not reusable:
        missing.append("No strong reusable Knowledge finding or documented policy was found.")
    if rag_result.get("status") == "not_configured":
        missing.append("RAG database search is not configured yet.")
    if web_result.get("status") == "not_configured":
        missing.append("Web search service is not configured yet.")
    recommendation = "use_reusable_knowledge"
    if missing or stale_or_weak:
        recommendation = "needs_fresh_check_or_policy_review"
    reusable_lines = [
        f"{doc['kind']} {doc['id']} query={doc['matched_query']} "
        f"reproducibility={doc['reproducibility']} title={doc['title']}"
        for doc in reusable
    ]
    stale_lines = [
        f"{doc['kind']} {doc['id']} signals={','.join(doc['stale_signals']) or doc['reproducibility']} "
        f"title={doc['title']}"
        for doc in stale_or_weak
    ]
    return (
        "# Knowledge Research Result\n\n"
        f"- schema: {KNOWLEDGE_RESEARCH_SCHEMA_VERSION}\n"
        f"- request_document_id: {request_doc.get('id')}\n"
        f"- source_run_id: {run.get('id')}\n"
        f"- ticket_id: {run.get('ticket_id') or ''}\n"
        f"- environment: {run.get('environment') or ''}\n"
        f"- machine: {run.get('machine') or ''}\n"
        f"- recommendation: {recommendation}\n"
        f"- answerable_from_db: {'yes' if reusable and not missing else 'no'}\n\n"
        "## Request\n"
        f"{_body_without_meta(str(request_doc.get('body_md') or ''), limit=1800)}\n\n"
        "## Queries\n"
        f"{_md_list(queries)}\n\n"
        "## Reusable Knowledge Candidates\n"
        f"{_md_list(reusable_lines)}\n\n"
        "## Stale Or Low-Context Candidates\n"
        f"{_md_list(stale_lines)}\n\n"
        "## Knowledge Excerpts\n"
        f"{json.dumps(classified, ensure_ascii=False, indent=2)}\n\n"
        "## RAG Search\n"
        f"{json.dumps(rag_result, ensure_ascii=False, indent=2)[:4000]}\n\n"
        "## Web Search\n"
        f"{json.dumps(web_result, ensure_ascii=False, indent=2)[:4000]}\n\n"
        "## Missing Evidence / Next Step\n"
        f"{_md_list(missing)}\n"
    )


def _attach_result(run: dict[str, Any], request_doc: dict[str, Any], body: str) -> str:
    created = common.knowledge_attach_run_document(str(run["id"]), {
        "role": "knowledge_research_result",
        "ticket_id": run.get("ticket_id"),
        "kind": "knowledge-research-result",
        "title": f"Knowledge research result for {request_doc.get('title') or request_doc.get('id')}",
        "summary": "DB-first knowledge research result with freshness and source notes.",
        "body_md": body,
        "tags": ["knowledge-research-result", "db-first", KNOWLEDGE_RESEARCH_SCHEMA_VERSION],
        "source": "zendesk-support-ai-knowledge-research-worker",
        "environment": run.get("environment") or "",
        "machine": run.get("machine") or "",
    })
    document = created.get("document") if isinstance(created, dict) else {}
    return str(document.get("id") or "")


def _pending_real_machine_or_policy(run_id: str) -> bool:
    try:
        docs = common.knowledge_list_run_documents(run_id, include_body=False)
    except Exception:
        return True
    kinds = {str(doc.get("kind") or "") for doc in docs}
    if "real-machine-investigation-request" in kinds or "policy-decision-request" in kinds:
        return True
    try:
        children = common.knowledge_list_runs(parent_run_id=run_id, limit=20)
    except Exception:
        return True
    return any(str(child.get("status") or "") not in TASK_DONE_STATUSES for child in children)


def _child_runs(parent_run_id: str) -> list[dict[str, Any]]:
    try:
        return common.knowledge_list_runs(parent_run_id=parent_run_id, limit=100)
    except Exception as exc:  # noqa: BLE001
        common.log(f"knowledge research parent child listing failed {parent_run_id}: {exc}")
        return []


def _task_done(run: dict[str, Any]) -> bool:
    return str(run.get("status") or "") in TASK_DONE_STATUSES


def _has_final_router_plan(parent_run_id: str) -> bool:
    try:
        docs = common.knowledge_list_run_documents(parent_run_id, include_body=True)
    except Exception:
        return False
    return any(
        doc.get("kind") == "investigation-router-plan"
        and "- routing_phase: final_split" in str(doc.get("body_md") or "")
        for doc in docs
    )


def _has_presearch_child(children: list[dict[str, Any]]) -> bool:
    for child in children:
        if child.get("task_type") != "knowledge_research":
            continue
        try:
            docs = common.knowledge_list_run_documents(str(child["id"]), include_body=True)
        except Exception:
            continue
        if any(PRESEARCH_SCHEMA_VERSION in str(doc.get("body_md") or "") for doc in docs):
            return True
    return False


def _maybe_advance_parent(parent_run_id: str, *, verbose: bool = False) -> None:
    if not parent_run_id:
        return
    children = _child_runs(parent_run_id)
    if not children:
        return
    knowledge_children = [child for child in children if child.get("task_type") == "knowledge_research"]
    policy_children = [child for child in children if child.get("task_type") == "policy_decision"]
    real_machine_scope_children = [child for child in children if child.get("task_type") == "real_machine_scope"]
    real_machine_children = [child for child in children if child.get("task_type") == "real_machine"]

    pending_knowledge = [child for child in knowledge_children if not _task_done(child)]
    if pending_knowledge:
        common.knowledge_update_run(parent_run_id, {
            "status": "investigation_waiting",
            "summary": f"Waiting for {len(pending_knowledge)} knowledge research task(s).",
        })
        return

    if _has_presearch_child(children) and not _has_final_router_plan(parent_run_id):
        common.knowledge_update_run(parent_run_id, {
            "status": "routing_requested",
            "summary": "Initial DB/Knowledge presearch completed; ready for final investigation routing.",
        })
        if verbose:
            common.log(f"knowledge research parent returned to routing {parent_run_id}: presearch complete")
        return

    promoted = 0
    for child in real_machine_scope_children:
        if child.get("status") == "investigation_waiting":
            common.knowledge_update_run(str(child["id"]), {
                "status": "split_requested",
                "summary": "Knowledge research completed; real-machine scope is ready for task splitting.",
            })
            promoted += 1

    for child in real_machine_children:
        if child.get("status") == "investigation_waiting":
            common.knowledge_update_run(str(child["id"]), {
                "status": "requested",
                "summary": "Knowledge research completed; real-machine task is ready for runbook planning.",
            })
            promoted += 1

    pending_real_machine_scope = [
        child
        for child in real_machine_scope_children
        if str(child.get("status") or "") not in {"closed", "done", "task_done", "superseded"}
    ]
    pending_real_machine = [
        child
        for child in real_machine_children
        if str(child.get("status") or "") not in {"closed", "done", "task_done", "answer_review", "result_registered"}
    ]
    pending_policy = [child for child in policy_children if not _task_done(child)]
    if pending_policy:
        status = "policy_review"
        summary = f"Waiting for {len(pending_policy)} policy decision task(s)."
    elif pending_real_machine_scope or pending_real_machine or promoted:
        status = "investigation_waiting"
        pending_count = len(pending_real_machine_scope) + len(pending_real_machine)
        summary = f"Waiting for {pending_count or promoted} real-machine scope/task(s)."
    else:
        status = "answer_review"
        summary = "All investigation tasks completed; ready for answer review."
    common.knowledge_update_run(parent_run_id, {"status": status, "summary": summary})
    if verbose:
        common.log(
            f"knowledge research parent advanced {parent_run_id}: "
            f"promoted={promoted} status={status}"
        )


def process_one(run_ref: dict[str, Any], *, verbose: bool = False) -> bool:
    run_id = str(run_ref["id"])
    try:
        run = common.knowledge_get_run(run_id)
        if run.get("status") not in {"investigation_waiting", "knowledge_researching"}:
            return False
        documents = common.knowledge_list_run_documents(run_id, include_body=True)
        requests = _latest_docs_by_kind(documents, "knowledge-research-request")
        pending = [doc for doc in requests if not _result_exists(documents, str(doc.get("id") or ""))]
        if not pending:
            if not _pending_real_machine_or_policy(run_id):
                common.knowledge_update_run(run_id, {
                    "status": "task_done" if run.get("task_type") == "knowledge_research" else "answer_review",
                    "summary": "Knowledge research completed; no pending real-machine or policy request found.",
                })
            return False
        common.knowledge_update_run(run_id, {"status": "knowledge_researching"})
        did_work = False
        for request_doc in pending[:3]:
            queries = _build_queries(run, request_doc)
            knowledge_docs = _search_knowledge(queries, exclude_ids={str(request_doc.get("id") or "")})
            main_query = " ".join(queries[:4])
            rag_result = _optional_search("rag", RAG_SEARCH_URL, RAG_API_KEY, main_query)
            web_result = _optional_search("web", WEB_SEARCH_URL, WEB_SEARCH_API_KEY, main_query)
            body = _research_body(
                run=run,
                request_doc=request_doc,
                queries=queries,
                knowledge_docs=knowledge_docs,
                rag_result=rag_result,
                web_result=web_result,
            )
            doc_id = _attach_result(run, request_doc, body)
            did_work = True
            if verbose:
                common.log(f"knowledge research result attached {run_id}: request={request_doc.get('id')} result={doc_id}")
        parent_run_id = str(run.get("parent_run_id") or "")
        if run.get("task_type") == "knowledge_research":
            next_status = "task_done"
        else:
            next_status = "investigation_waiting" if _pending_real_machine_or_policy(run_id) else "answer_review"
        common.knowledge_update_run(run_id, {
            "status": next_status,
            "summary": f"Knowledge research processed {len(pending[:3])} request(s).",
        })
        if parent_run_id:
            _maybe_advance_parent(parent_run_id, verbose=verbose)
        return did_work
    except Exception as exc:  # noqa: BLE001
        common.log(f"knowledge research failed {run_id}: {exc}")
        try:
            common.knowledge_update_run(run_id, {
                "status": "human_review",
                "issue_on_run": f"knowledge research worker failed: {exc}",
            })
        except Exception as update_exc:  # noqa: BLE001
            common.log(f"knowledge research failed to mark {run_id}: {update_exc}")
        return False


def run_once(verbose: bool = False, limit: int = 5) -> int:
    if not KNOWLEDGE_RESEARCH_WORKER_ENABLED:
        if verbose:
            common.log("knowledge research worker disabled")
        return 0
    if not common.knowledge_enabled():
        if verbose:
            common.log("knowledge research skipped: Knowledge API not configured")
        return 0
    runs = []
    for _ in range(limit):
        run = common.knowledge_worker_claim_run(
            worker="knowledge-research-worker",
            statuses=["investigation_waiting"],
            claim_status="knowledge_researching",
            task_type="knowledge_research",
        )
        if not run:
            break
        runs.append(run)
    n = 0
    for run in runs:
        if process_one(run, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"knowledge research worker done: {len(runs)} candidate runs, {n} researched")
    return n


def run_forever(verbose: bool = False, interval: int = 60, limit: int = 5) -> None:
    common.log(f"knowledge research worker start (interval={interval}s limit={limit})")
    while True:
        try:
            run_once(verbose=verbose, limit=limit)
        except Exception as exc:  # noqa: BLE001
            common.log(f"knowledge research loop error (continuing): {exc}")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge research worker")
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
