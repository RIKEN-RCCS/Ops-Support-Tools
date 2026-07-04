#!/usr/bin/env python3
"""プロセス A: 生成(spec フェーズ5)。

incoming/ を消費 → チケット本文取得(読取専用)→ PII マスキング → ローカル LLM で作文
→ pending/ に JSON 保存。

このプロセスは Zendesk への書き込み権限を持たない(common には post_internal_note があるが、
generator はそれを呼ばない)。実運用では読取専用ユーザーで動かす(spec §6-3)。
"""

from __future__ import annotations

import argparse
import os
import time

import common
import llm_client
import pii_mask
import target_normalizer


SUPPORT_AI_TRIAGE_TAG = os.environ.get("SUPPORT_AI_TRIAGE_TAG", "ai_triaged")
CREATE_KNOWLEDGE_RUNS = os.environ.get("SUPPORT_AI_CREATE_KNOWLEDGE_RUNS", "1").lower() in ("1", "true", "yes")


def _extract_texts(ticket_id: int):
    """subject と コメント body のみを allowlist 抽出する(spec §5)。

    author_id / via / metadata / 添付等は捨てる。Comments API を使う。
    戻り値: (subject, bodies, already_triaged)。
    """
    ticket = common.fetch_ticket(ticket_id)
    # Search インデックスの遅延で投稿済みチケットが再取得されることがある。
    # ライブのタグを確認し、二重投稿を防ぐ(spec §10 Search API の重複対策)。
    if SUPPORT_AI_TRIAGE_TAG in (ticket.get("tags") or []):
        return None, None, True
    subject = ticket.get("subject") or ""
    comments = common.fetch_ticket_comments(ticket_id)
    bodies = []
    for c in comments:
        # plain_body 優先(spec §5)
        body = c.get("plain_body") or c.get("body") or ""
        if body:
            bodies.append(body)
    return subject, bodies, False


def _yesno(value: object) -> str:
    return "yes" if bool(value) else "no"


def _build_runbook(triage: dict) -> str:
    """環境依存の問い合わせを後続AI/人間へ渡すための初期runbook。"""
    return (
        "# Investigation Case Request\n\n"
        "## Goal\n"
        "チケット内容に対して、公開返信前に必要な環境固有情報・既存知見・実機状態を確認し、"
        "根拠付きの回答案を作成する。\n\n"
        "## Inputs\n"
        "- Zendesk ticket_id は run metadata を参照する。\n"
        "- environment / machine が未設定の場合は、本文、タグ、運用文脈から推定し、"
        "不明なら findings に `environment_unknown` と明記する。\n"
        f"- target_resolution: {triage.get('target_resolution', '')}\n"
        f"- target_resolution_reason: {triage.get('target_resolution_reason', '')}\n"
        "- 公開返信は行わない。回答案は `answer_draft` として Knowledge に登録する。\n\n"
        "## Safety Gate\n"
        "1. 破壊的操作、設定変更、ジョブ投入、ユーザーデータ参照、管理者権限が必要な操作は、"
        "実行前に `requires_operator_approval` として停止する。\n"
        "2. 実機確認は読み取り系コマンドを優先し、取得した事実と推測を分けて記録する。\n"
        "3. 環境の提供方針やサポート範囲が不明な場合、一般論で断定せず担当者確認へ戻す。\n\n"
        "## Execution Steps\n"
        "1. Knowledge API で同じ category、environment、machine、関連タグの既存 documents / runs / handoffs を確認する。\n"
        "2. チケット本文から、ユーザーが本当に聞いている判断点を1文で整理する。\n"
        "3. 必要な環境情報を確認する。例: module、コンパイラ、CUDA、MPI、ライブラリ、ジョブ環境、"
        "既知制約、運用方針、サポート範囲。\n"
        "4. 確認結果から、推奨方針、代替案、追加質問の要否を判断する。\n"
        "5. 解決できる場合は、根拠付きの回答案を作る。解決できない場合は、追加調査runbookまたは担当者確認事項を作る。\n\n"
        "## Required Outputs\n"
        "Knowledge のこの run に、少なくとも次の document を登録する。\n"
        "- `findings`: 確認した事実、参照した知見、環境情報、未確認事項。\n"
        "- `issue_on_run`: 実行中に起きた問題、権限不足、確認不能だった点。問題がなければ `none`。\n"
        "- `summary`: 結論、根拠、残リスク、次アクション。\n"
        "- `answer_draft`: Zendeskへ戻す社内メモ案または公開返信案。公開可否を明記する。\n\n"
        "## Failed-Run Loop\n"
        "この run で解決できない場合は、`summary` に不足情報と次に必要な確認を明記し、"
        "追加investigation task生成または `operator-review` handoff に回す。\n\n"
        "## Initial AI Triage\n"
        f"- summary: {triage.get('summary', '').strip()}\n"
        f"- probable_cause: {triage.get('probable_cause', '').strip()}\n"
        f"- category: {triage.get('category')}\n"
        "- urgency: not_assessed\n"
        f"- difficulty: {triage.get('difficulty')}\n"
        f"- answer_confidence: {triage.get('answer_confidence')}\n"
        f"- suggested_next_action: {triage.get('suggested_next_action', '').strip()}\n"
    )


def _decision_document_body(
    *,
    decision: str,
    case_change: str,
    triage: dict,
    reason: str = "",
    case_delta: str = "",
    answer_draft_policy: str = "",
) -> str:
    return (
        "# Investigation Case/Task Decision\n\n"
        f"- investigation_decision: {decision}\n"
        f"- case_change: {case_change}\n"
        f"- answer_draft_policy: {answer_draft_policy or ('draft' if triage.get('safe_to_reply_to_user') else 'hold')}\n\n"
        "## Reason\n"
        f"{reason or '同じ Zendesk ticket の未完了 investigation case に対する case/task decision。'}\n\n"
        "## Investigation Delta\n"
        f"{case_delta or 'none'}\n\n"
        "## Added Context\n"
        f"- summary: {triage.get('summary', '').strip()}\n"
        f"- probable_cause: {triage.get('probable_cause', '').strip()}\n"
        f"- category: {triage.get('category')}\n"
        f"- difficulty: {triage.get('difficulty')}\n"
        f"- answer_confidence: {triage.get('answer_confidence')}\n"
        f"- suggested_next_action: {triage.get('suggested_next_action', '').strip()}\n"
    )


def _attach_decision_document(
    ticket_id: int,
    run_id: str,
    triage: dict,
    *,
    decision: dict,
) -> None:
    investigation_decision = str(decision.get("investigation_decision") or "attach_to_existing_run")
    case_change = str(decision.get("case_change") or "append_context")
    common.knowledge_attach_run_document(run_id, {
        "role": "case_decision",
        "ticket_id": ticket_id,
        "kind": "case-decision",
        "title": f"Investigation case/task decision for ticket {ticket_id}",
        "summary": f"{investigation_decision}; case_change={case_change}.",
        "body_md": _decision_document_body(
            decision=investigation_decision,
            case_change=case_change,
            triage=triage,
            reason=str(decision.get("reason") or ""),
            case_delta=str(decision.get("case_delta") or ""),
            answer_draft_policy=str(decision.get("answer_draft_policy") or ""),
        ),
        "tags": ["case-decision", "triage", str(triage.get("category") or "other")],
        "source": "zendesk-support-ai",
    })


def _runbook_with_decision_delta(triage: dict, decision: dict) -> str:
    runbook = _build_runbook(triage)
    delta = str(decision.get("case_delta") or "").strip()
    reason = str(decision.get("reason") or "").strip()
    if not delta and not reason:
        return runbook
    return (
        f"{runbook}\n\n"
        "## Investigation Decision Delta\n"
        f"- investigation_decision: {decision.get('investigation_decision')}\n"
        f"- case_change: {decision.get('case_change')}\n"
        f"- reason: {reason or 'none'}\n"
        f"- case_delta: {delta or 'none'}\n"
    )


def _resolved_target(event: dict, triage: dict) -> tuple[str, str, str, str]:
    environment = str(event.get("environment") or "").strip()
    raw_machine = str(event.get("machine") or "").strip()
    machine, machine_status = target_normalizer.canonicalize_machine(raw_machine)
    if raw_machine and machine_status in {"unknown", "ambiguous"}:
        return environment, "", "operator_select", (
            f"Zendesk webhook payload の machine={raw_machine!r} を一意なcanonical machineへ正規化できないため。"
        )
    if environment or machine:
        return environment, machine, "identified_from_webhook", (
            f"Zendesk webhook payload に対象情報が含まれていたため。machine_status={machine_status}"
        )
    if not environment and triage.get("environment_confidence") == "high":
        environment = str(triage.get("environment") or "").strip()
    if not machine and triage.get("machine_confidence") == "high":
        raw_triage_machine = str(triage.get("machine") or "").strip()
        machine, machine_status = target_normalizer.canonicalize_machine(raw_triage_machine)
        if raw_triage_machine and not machine:
            triage["machine_confidence"] = "low"
    if environment or machine:
        return environment, machine, "identified_from_text", (
            f"triage AI が本文から高確信で対象候補を特定したため。machine_status={machine_status}"
        )
    return (
        environment,
        machine,
        str(triage.get("target_resolution") or "ask_user"),
        str(triage.get("target_resolution_reason") or ""),
    )


def _create_knowledge_run(ticket_id: int, triage: dict, *, environment: str = "", machine: str = "") -> tuple[str | None, str | None, str, str]:
    if not CREATE_KNOWLEDGE_RUNS or not common.knowledge_enabled():
        return None, None, "no_runbook_needed", "none"
    if not (triage.get("requires_runbook") or triage.get("requires_environment_knowledge")):
        return None, None, "no_runbook_needed", "none"
    payload = {
        "ticket_id": ticket_id,
        "environment": environment,
        "machine": machine,
        "task_type": "investigation_case",
        "task_priority": "normal",
        "status": "routing_requested",
        "runbook": _build_runbook(triage),
        "summary": triage.get("summary", ""),
    }
    try:
        created_case_change = "initial"
        existing = common.knowledge_find_requested_run(ticket_id)
        if existing and existing.get("id"):
            decision = llm_client.case_decision(
                source="triage",
                ticket_id=ticket_id,
                analysis=triage,
                existing_run=existing,
            )
            decision = llm_client.normalize_case_decision(decision)
            investigation_decision = str(decision.get("investigation_decision") or "operator_review")
            case_change = str(decision.get("case_change") or "none")
            run_id = str(existing["id"])
            if investigation_decision == "attach_to_existing_run":
                _attach_decision_document(ticket_id, run_id, triage, decision=decision)
                common.knowledge_update_run(run_id, {"status": "routing_requested"})
                return run_id, None, investigation_decision, case_change
            if investigation_decision == "no_runbook_needed":
                _attach_decision_document(ticket_id, run_id, triage, decision=decision)
                return None, None, investigation_decision, case_change
            if investigation_decision == "operator_review":
                _attach_decision_document(ticket_id, run_id, triage, decision=decision)
                return run_id, None, investigation_decision, case_change
            payload["runbook"] = _runbook_with_decision_delta(triage, decision)
            payload["summary"] = f"{triage.get('summary', '')} ({case_change})"
            created_case_change = case_change
        created = common.knowledge_create_run(payload)
        run = created.get("run") if isinstance(created, dict) else {}
        run_id = run.get("id") if isinstance(run, dict) else None
        return str(run_id) if run_id else None, None, "open_new_investigation", created_case_change
    except Exception as exc:  # noqa: BLE001
        return None, str(exc), "operator_review", "none"


def _build_note_body(triage: dict, model: str, *, knowledge_run_id: str | None = None,
                     knowledge_run_error: str | None = None, investigation_decision: str = "",
                     case_change: str = "") -> str:
    """トリアージ結果から社内メモ本文を組み立てる(プレースホルダは残したまま)。"""
    qs = triage.get("clarifying_questions") or []
    q_lines = "\n".join(f"- {q}" for q in qs) if qs else "- (なし)"
    safe_to_reply = bool(triage.get("safe_to_reply_to_user"))
    if safe_to_reply:
        draft_label = "■ 一次返信ドラフト"
        draft_body = triage.get("draft_reply", "").strip()
    else:
        draft_label = "■ 一次返信ドラフト(公開返信への利用は保留)"
        draft_body = (
            "この問い合わせは環境固有情報、既存知見、または実機確認が必要な可能性があります。\n"
            "以下の文案をそのまま公開返信に使わず、Knowledge/runbook確認後に回答案を作成してください。\n\n"
            + triage.get("draft_reply", "").strip()
        )
    run_line = ""
    if knowledge_run_id:
        run_line = (
            "\n■ Knowledge run\n"
            f"investigation_decision: {investigation_decision or 'open_new_investigation'}\n"
            f"case_change: {case_change or 'initial'}\n"
            f"run_id: {knowledge_run_id}\n"
        )
    elif knowledge_run_error:
        run_line = "\n■ Knowledge run\n作成失敗: 後続で手動作成してください。\n"
    return (
        f"🤖 AI 一次トリアージ(自動生成・参考情報 / model: {model})\n"
        "─────────────────────────────\n"
        f"■ 緊急度: 判断対象外　／　カテゴリ: {triage.get('category')}"
        f"　／　難易度: {triage.get('difficulty')}\n\n"
        "■ 回答ゲート\n"
        f"- answer_confidence: {triage.get('answer_confidence')}\n"
        f"- safe_to_reply_to_user: {_yesno(triage.get('safe_to_reply_to_user'))}\n"
        f"- requires_environment_knowledge: {_yesno(triage.get('requires_environment_knowledge'))}\n"
        f"- requires_runbook: {_yesno(triage.get('requires_runbook'))}\n"
        f"- requires_operator_check: {_yesno(triage.get('requires_operator_check'))}\n"
        f"- suggested_next_action: {triage.get('suggested_next_action', '').strip()}\n"
        f"- target_resolution: {triage.get('target_resolution', '')}\n"
        f"- target_resolution_reason: {triage.get('target_resolution_reason', '')}\n"
        f"- environment: {triage.get('environment', '')} ({triage.get('environment_confidence', '')})\n"
        f"- machine: {triage.get('machine', '')} ({triage.get('machine_confidence', '')})\n"
        f"{run_line}\n"
        f"■ 要約\n{triage.get('summary', '').strip()}\n\n"
        f"■ 推定原因\n{triage.get('probable_cause', '').strip()}\n\n"
        f"■ ユーザーへの追加確認事項\n{q_lines}\n\n"
        f"{draft_label}\n{draft_body}\n"
        "─────────────────────────────\n"
        "※ 担当はシステムが決定し、投稿時にこのメモ末尾へ追記されます。\n"
        f"※ このメモは {model} により自動生成されました。"
        "緊急度・優先度は判断対象外です。難易度は参考情報です。アサイン・返信は人間が判断してください。"
    )


def process_one(path, verbose: bool = False) -> bool:
    """incoming/ の 1 ファイルを処理して pending/ に出す。成功で True。"""
    event = common.read_json(path)
    ticket_id = int(event["ticket_id"])
    if verbose:
        common.log(f"generating ticket_{ticket_id}")

    try:
        subject, bodies, already = _extract_texts(ticket_id)
        if already:
            # 既に投稿済み。生成せず incoming から取り除く(LLM 呼び出しも省く)
            if verbose:
                common.log(f"skip ticket_{ticket_id}: already tagged '{SUPPORT_AI_TRIAGE_TAG}'")
            path.unlink()
            return False
        # subject と 全 body を一貫マッピングでマスク
        fields = [subject] + bodies
        masked, mapping = pii_mask.mask_fields(fields)
        masked_subject = masked[0]
        masked_body = "\n\n---\n\n".join(masked[1:]) if len(masked) > 1 else ""

        triage = llm_client.triage(masked_subject, masked_body)
        model = triage.get("_model", "unknown")
        environment, machine, target_resolution, target_resolution_reason = _resolved_target(event, triage)
        triage["environment"] = environment
        triage["machine"] = machine
        triage["target_resolution"] = target_resolution
        triage["target_resolution_reason"] = target_resolution_reason
        knowledge_run_id, knowledge_run_error, investigation_decision, case_change = _create_knowledge_run(
            ticket_id,
            triage,
            environment=environment,
            machine=machine,
        )
        if knowledge_run_error and verbose:
            common.log(f"knowledge run create failed ticket_{ticket_id}: {knowledge_run_error}")

        record = {
            "ticket_id": ticket_id,
            "generated_at": int(time.time()),
            "model": model,
            "severity": "not_assessed",
            "category": triage["category"],
            "difficulty": triage["difficulty"],  # poster の割り当てロジックで使う
            "answer_confidence": triage["answer_confidence"],
            "requires_environment_knowledge": triage["requires_environment_knowledge"],
            "requires_runbook": triage["requires_runbook"],
            "requires_operator_check": triage["requires_operator_check"],
            "safe_to_reply_to_user": triage["safe_to_reply_to_user"],
            "suggested_next_action": triage["suggested_next_action"],
            "environment": environment,
            "machine": machine,
            "environment_confidence": triage.get("environment_confidence"),
            "machine_confidence": triage.get("machine_confidence"),
            "target_resolution": target_resolution,
            "target_resolution_reason": target_resolution_reason,
            "knowledge_run_id": knowledge_run_id,
            "investigation_decision": investigation_decision,
            "case_change": case_change,
            "note_body": _build_note_body(
                triage,
                model,
                knowledge_run_id=knowledge_run_id,
                knowledge_run_error=knowledge_run_error,
                investigation_decision=investigation_decision,
                case_change=case_change,
            ),
            "mask_mapping": mapping,  # ローカル保持のみ。LLM にも外部にも送らない
        }
        common.atomic_write_json(common.spool_path("pending") / f"ticket_{ticket_id}.json", record)
        # 処理済み incoming はアーカイブ(done ではなく削除でもよいが監査のため failed と区別)
        path.unlink()
        if verbose:
            common.log(f"-> pending/ticket_{ticket_id}.json "
                       f"(urgency:not_assessed/{record['category']} via {model})")
        return True
    except Exception as e:
        common.log(f"generate failed ticket_{ticket_id}: {e}")
        # 失敗イベントは failed/ へ退避(人間が調査)
        try:
            common.move_to(path, "failed")
        except OSError:
            pass
        return False


def run_once(verbose: bool = False) -> int:
    common.ensure_spool_dirs()
    files = common.list_queue("incoming", "ticket_*.json")
    n = 0
    for f in files:
        if process_one(f, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"generator done: {len(files)} consumed, {n} generated")
    return n


def run_forever(verbose: bool = False, interval: int = 30) -> None:
    common.log(f"generator start (interval={interval}s)")
    while True:
        try:
            run_once(verbose=verbose)
        except Exception as e:
            common.log(f"generator loop error (continuing): {e}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Triage generator (process A)")
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
