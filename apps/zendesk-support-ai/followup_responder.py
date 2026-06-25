#!/usr/bin/env python3
"""Follow-up responder.

pending_followup/ を消費し、追加質問への返信ドラフトを内部メモとして投稿する。
公開返信は行わない。
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Optional

import common
import llm_client
import pii_mask


FOLLOWUP_DRAFTED_TAG = "ai_followup_drafted"
CREATE_KNOWLEDGE_RUNS = os.environ.get("SUPPORT_AI_CREATE_KNOWLEDGE_RUNS", "1").lower() in ("1", "true", "yes")
MAX_NOTE_LEN = 30000
MIN_NOTE_LEN = 20


class ValidationError(Exception):
    pass


def _comment_body(comment: dict[str, Any]) -> str:
    return str(comment.get("plain_body") or comment.get("body") or "").strip()


def _find_comment(comments: list[dict[str, Any]], comment_id: Optional[int]) -> Optional[dict[str, Any]]:
    if comment_id is None:
        public_comments = [c for c in comments if c.get("public") is True]
        return public_comments[-1] if public_comments else None
    for comment in comments:
        try:
            if int(comment.get("id")) == int(comment_id):
                return comment
        except (TypeError, ValueError):
            continue
    return None


def _public_conversation(comments: list[dict[str, Any]], *, requester_id: Optional[int], limit: int = 12) -> str:
    public_comments = [c for c in comments if c.get("public") is True and _comment_body(c)]
    lines = []
    for comment in public_comments[-limit:]:
        try:
            author_id = int(comment.get("author_id"))
        except (TypeError, ValueError):
            author_id = None
        speaker = "end_user" if requester_id is not None and author_id == requester_id else "support"
        created = comment.get("created_at") or ""
        lines.append(f"[{created} speaker={speaker}]\n{_comment_body(comment)}")
    return "\n\n---\n\n".join(lines)


def _validate_result(result: dict[str, Any]) -> None:
    if not isinstance(result.get("summary"), str):
        raise ValidationError("summary が string でない")
    if not isinstance(result.get("answerable"), bool):
        raise ValidationError("answerable が bool でない")
    if not isinstance(result.get("needs_agent_review"), bool):
        raise ValidationError("needs_agent_review が bool でない")
    for key in ("requires_environment_knowledge", "requires_runbook", "safe_to_reply_to_user"):
        if not isinstance(result.get(key), bool):
            raise ValidationError(f"{key} が bool でない")
    if result.get("answer_confidence") not in llm_client.ANSWER_CONFIDENCES:
        raise ValidationError(f"不正な answer_confidence: {result.get('answer_confidence')!r}")
    if not isinstance(result.get("suggested_next_action"), str):
        raise ValidationError("suggested_next_action が string でない")
    body = result.get("draft_reply")
    if not isinstance(body, str) or not (MIN_NOTE_LEN <= len(body) <= MAX_NOTE_LEN):
        raise ValidationError(f"draft_reply の長さが不正: {len(body) if isinstance(body, str) else 'N/A'}")
    if not isinstance(result.get("agent_note"), str):
        raise ValidationError("agent_note が string でない")


def _yesno(value: object) -> str:
    return "yes" if bool(value) else "no"


def _build_runbook(result: dict[str, Any]) -> str:
    return (
        "# Followup Runbook Request\n\n"
        "## Goal\n"
        "追加質問に対して、公開返信前に必要な環境固有情報・既存知見・実機状態を確認し、"
        "根拠付きの回答案を作成する。\n\n"
        "## Safety Gate\n"
        "1. 破壊的操作、設定変更、ジョブ投入、ユーザーデータ参照、管理者権限が必要な操作は、"
        "実行前に `requires_operator_approval` として停止する。\n"
        "2. 実機確認は読み取り系コマンドを優先し、取得した事実と推測を分けて記録する。\n"
        "3. 環境の提供方針やサポート範囲が不明な場合、一般論で断定せず担当者確認へ戻す。\n\n"
        "## Required Outputs\n"
        "- `findings`: 確認した事実、参照した知見、環境情報、未確認事項。\n"
        "- `issue_on_run`: 実行中の問題。問題がなければ `none`。\n"
        "- `summary`: 結論、根拠、残リスク、次アクション。\n"
        "- `answer_draft`: Zendeskへ戻す社内メモ案または公開返信案。公開可否を明記する。\n\n"
        "## Initial Followup Triage\n"
        f"- summary: {result.get('summary', '').strip()}\n"
        f"- answer_confidence: {result.get('answer_confidence')}\n"
        f"- suggested_next_action: {result.get('suggested_next_action', '').strip()}\n"
    )


def _decision_document_body(
    *,
    decision: str,
    runbook_change: str,
    result: dict[str, Any],
    reason: str = "",
    runbook_delta: str = "",
    answer_draft_policy: str = "",
) -> str:
    return (
        "# Followup Runbook Decision\n\n"
        f"- investigation_decision: {decision}\n"
        f"- runbook_change: {runbook_change}\n"
        f"- answer_draft_policy: {answer_draft_policy or ('draft' if result.get('safe_to_reply_to_user') else 'hold')}\n\n"
        "## Reason\n"
        f"{reason or '同じ Zendesk ticket の未完了 run に対する followup runbook decision。'}\n\n"
        "## Runbook Delta\n"
        f"{runbook_delta or 'none'}\n\n"
        "## Added Followup Context\n"
        f"- summary: {result.get('summary', '').strip()}\n"
        f"- answer_confidence: {result.get('answer_confidence')}\n"
        f"- suggested_next_action: {result.get('suggested_next_action', '').strip()}\n"
    )


def _attach_decision_document(
    ticket_id: int,
    run_id: str,
    result: dict[str, Any],
    *,
    decision: dict[str, Any],
) -> None:
    investigation_decision = str(decision.get("investigation_decision") or "attach_to_existing_run")
    runbook_change = str(decision.get("runbook_change") or "append_context")
    common.knowledge_attach_run_document(run_id, {
        "role": "runbook_decision",
        "ticket_id": ticket_id,
        "kind": "runbook-decision",
        "title": f"Followup runbook decision for ticket {ticket_id}",
        "summary": f"{investigation_decision}; runbook_change={runbook_change}.",
        "body_md": _decision_document_body(
            decision=investigation_decision,
            runbook_change=runbook_change,
            result=result,
            reason=str(decision.get("reason") or ""),
            runbook_delta=str(decision.get("runbook_delta") or ""),
            answer_draft_policy=str(decision.get("answer_draft_policy") or ""),
        ),
        "tags": ["runbook-decision", "followup"],
        "source": "zendesk-support-ai",
    })


def _runbook_with_decision_delta(result: dict[str, Any], decision: dict[str, Any]) -> str:
    runbook = _build_runbook(result)
    delta = str(decision.get("runbook_delta") or "").strip()
    reason = str(decision.get("reason") or "").strip()
    if not delta and not reason:
        return runbook
    return (
        f"{runbook}\n\n"
        "## Runbook Decision Delta\n"
        f"- investigation_decision: {decision.get('investigation_decision')}\n"
        f"- runbook_change: {decision.get('runbook_change')}\n"
        f"- reason: {reason or 'none'}\n"
        f"- runbook_delta: {delta or 'none'}\n"
    )


def _create_knowledge_run(ticket_id: int, result: dict[str, Any]) -> tuple[str | None, str | None, str, str]:
    if not CREATE_KNOWLEDGE_RUNS or not common.knowledge_enabled():
        return None, None, "no_runbook_needed", "none"
    if not (result.get("requires_runbook") or result.get("requires_environment_knowledge")):
        return None, None, "no_runbook_needed", "none"
    payload = {
        "ticket_id": ticket_id,
        "status": "requested",
        "runbook": _build_runbook(result),
        "summary": result.get("summary", ""),
    }
    try:
        created_runbook_change = "initial"
        existing = common.knowledge_find_requested_run(ticket_id)
        if existing and existing.get("id"):
            decision = llm_client.runbook_decision(
                source="followup",
                ticket_id=ticket_id,
                analysis=result,
                existing_run=existing,
            )
            decision = llm_client.normalize_runbook_decision(decision)
            investigation_decision = str(decision.get("investigation_decision") or "operator_review")
            runbook_change = str(decision.get("runbook_change") or "none")
            run_id = str(existing["id"])
            if investigation_decision == "attach_to_existing_run":
                _attach_decision_document(ticket_id, run_id, result, decision=decision)
                return run_id, None, investigation_decision, runbook_change
            if investigation_decision == "no_runbook_needed":
                _attach_decision_document(ticket_id, run_id, result, decision=decision)
                return None, None, investigation_decision, runbook_change
            if investigation_decision == "operator_review":
                _attach_decision_document(ticket_id, run_id, result, decision=decision)
                return run_id, None, investigation_decision, runbook_change
            payload["runbook"] = _runbook_with_decision_delta(result, decision)
            payload["summary"] = f"{result.get('summary', '')} ({runbook_change})"
            created_runbook_change = runbook_change
        created = common.knowledge_create_run(payload)
        run = created.get("run") if isinstance(created, dict) else {}
        run_id = run.get("id") if isinstance(run, dict) else None
        return str(run_id) if run_id else None, None, "open_new_investigation", created_runbook_change
    except Exception as exc:  # noqa: BLE001
        return None, str(exc), "operator_review", "none"


def _build_internal_note(
    result: dict[str, Any],
    *,
    model: str,
    target_comment_id: Optional[int],
    knowledge_run_id: str | None = None,
    knowledge_run_error: str | None = None,
    investigation_decision: str = "",
    runbook_change: str = "",
) -> str:
    answerable = "yes" if result.get("answerable") else "no"
    review = "yes" if result.get("needs_agent_review") else "no"
    comment_line = target_comment_id if target_comment_id is not None else "(未指定)"
    safe_to_reply = bool(result.get("safe_to_reply_to_user"))
    if safe_to_reply:
        draft_label = "■ 返信ドラフト"
        draft_body = result.get("draft_reply", "").strip()
    else:
        draft_label = "■ 返信ドラフト(公開返信への利用は保留)"
        draft_body = (
            "この追加質問は環境固有情報、既存知見、または実機確認が必要な可能性があります。\n"
            "以下の文案をそのまま公開返信に使わず、Knowledge/runbook確認後に回答案を作成してください。\n\n"
            + result.get("draft_reply", "").strip()
        )
    run_line = ""
    if knowledge_run_id:
        run_line = (
            "\n■ Knowledge run\n"
            f"investigation_decision: {investigation_decision or 'open_new_investigation'}\n"
            f"runbook_change: {runbook_change or 'initial'}\n"
            f"run_id: {knowledge_run_id}\n"
        )
    elif knowledge_run_error:
        run_line = "\n■ Knowledge run\n作成失敗: 後続で手動作成してください。\n"
    return (
        f"🤖 AI Followup 返信ドラフト(自動生成・参考情報 / model: {model})\n"
        "─────────────────────────────\n"
        f"■ 対象 comment_id: {comment_line}\n"
        f"■ この情報だけで回答可能: {answerable}　／　担当者確認推奨: {review}\n\n"
        "■ 回答ゲート\n"
        f"- answer_confidence: {result.get('answer_confidence')}\n"
        f"- safe_to_reply_to_user: {_yesno(result.get('safe_to_reply_to_user'))}\n"
        f"- requires_environment_knowledge: {_yesno(result.get('requires_environment_knowledge'))}\n"
        f"- requires_runbook: {_yesno(result.get('requires_runbook'))}\n"
        f"- suggested_next_action: {result.get('suggested_next_action', '').strip()}\n"
        f"{run_line}\n"
        f"■ 追加質問の要約\n{result.get('summary', '').strip()}\n\n"
        f"{draft_label}\n{draft_body}\n\n"
        f"■ 担当者向け補足\n{result.get('agent_note', '').strip()}\n"
        "─────────────────────────────\n"
        "※ このメモは自動生成された返信案です。公開返信前に担当者が確認してください。"
    )


def _store_done_payload(path: common.QueueItem | Path, payload: dict[str, Any]) -> None:
    if isinstance(path, common.QueueItem):
        common.update_queue_item(path, payload)
    else:
        common.atomic_write_json_same_dir(Path(path), payload)


def _is_transient_error(exc: Exception) -> bool:
    text = str(exc)
    transient_markers = (
        "-> 408",
        "-> 429",
        "-> 500",
        "-> 502",
        "-> 503",
        "-> 504",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "全モデルで失敗しました",
    )
    return any(marker in text for marker in transient_markers)


def process_one(path, verbose: bool = False) -> bool:
    event = common.read_json(path)
    ticket_id = int(event["ticket_id"])
    raw_comment_id = event.get("comment_id")
    comment_id = int(raw_comment_id) if raw_comment_id is not None else None
    event["job"] = "followup"
    event["processing_started_at"] = int(time.time())
    event["attempt_count"] = int(event.get("attempt_count") or 0) + 1
    _store_done_payload(path, event)

    try:
        if verbose:
            common.log(f"followup responder processing ticket_{ticket_id} comment_{comment_id} attempt={event['attempt_count']}")
        ticket = common.fetch_ticket(ticket_id)
        comments = common.fetch_ticket_comments(ticket_id)
        target = _find_comment(comments, comment_id)
        if not target:
            raise RuntimeError(f"target followup comment not found: {comment_id}")

        subject = ticket.get("subject") or ""
        requester_id = int(ticket["requester_id"]) if ticket.get("requester_id") is not None else None
        conversation = _public_conversation(comments, requester_id=requester_id)
        followup_body = _comment_body(target)
        masked, mapping = pii_mask.mask_fields([subject, conversation, followup_body])

        result = llm_client.followup_reply(masked[0], masked[1], masked[2])
        _validate_result(result)
        model = result.get("_model", "unknown")
        knowledge_run_id, knowledge_run_error, investigation_decision, runbook_change = _create_knowledge_run(ticket_id, result)
        if knowledge_run_error and verbose:
            common.log(f"followup knowledge run create failed ticket_{ticket_id}: {knowledge_run_error}")

        note = _build_internal_note(
            result,
            model=model,
            target_comment_id=comment_id,
            knowledge_run_id=knowledge_run_id,
            knowledge_run_error=knowledge_run_error,
            investigation_decision=investigation_decision,
            runbook_change=runbook_change,
        )
        if pii_mask.has_unresolved_placeholders(note, mapping):
            raise ValidationError("対応表に無いプレースホルダ(捏造)が本文に残っている")
        final_note = pii_mask.unmask(note, mapping)
        if pii_mask.has_unresolved_placeholders(final_note, {}):
            raise ValidationError("unmask 後もプレースホルダが残っている")

        common.post_internal_note(ticket_id, final_note, tags=[FOLLOWUP_DRAFTED_TAG])
        done_payload = {
            **event,
            "job": "followup",
            "generated_at": int(time.time()),
            "model": model,
            "summary": result["summary"],
            "answerable": result["answerable"],
            "needs_agent_review": result["needs_agent_review"],
            "answer_confidence": result["answer_confidence"],
            "requires_environment_knowledge": result["requires_environment_knowledge"],
            "requires_runbook": result["requires_runbook"],
            "safe_to_reply_to_user": result["safe_to_reply_to_user"],
            "suggested_next_action": result["suggested_next_action"],
            "knowledge_run_id": knowledge_run_id,
            "investigation_decision": investigation_decision,
            "runbook_change": runbook_change,
            "note_body": note,
            "mask_mapping": mapping,
        }
        _store_done_payload(path, done_payload)
        common.move_to(path, "done")
        if verbose:
            common.log(f"followup drafted ticket_{ticket_id} comment_{comment_id} -> done/")
        return True
    except Exception as e:  # noqa: BLE001
        common.log(f"followup responder failed ticket_{ticket_id}: {e}")
        if _is_transient_error(e):
            common.log(f"followup responder will retry ticket_{ticket_id}: transient error")
            return False
        try:
            common.move_to(path, "failed")
        except OSError:
            pass
        return False


def run_once(verbose: bool = False) -> int:
    common.ensure_spool_dirs()
    files = common.list_queue("pending_followup", "ticket_*.json")
    n = 0
    for path in files:
        if process_one(path, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"followup responder done: {len(files)} pending, {n} drafted")
    return n


def run_forever(verbose: bool = False, interval: int = 30) -> None:
    common.log(f"followup responder start (interval={interval}s)")
    while True:
        try:
            run_once(verbose=verbose)
        except Exception as e:  # noqa: BLE001
            common.log(f"followup responder loop error (continuing): {e}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Follow-up responder")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    if args.once:
        run_once(verbose=args.verbose)
    else:
        run_forever(verbose=args.verbose, interval=args.interval)


if __name__ == "__main__":
    main()
