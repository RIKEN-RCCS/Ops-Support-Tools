#!/usr/bin/env python3
"""プロセス B: 投稿。

pending/ を消費 → スキーマ検証 → unmask → 社内メモ投稿(public:false)+ タグ
→ 担当者を決定論的に決定しカスタムフィールドへ書込 → done/。
検証 NG / 捏造プレースホルダは failed/ へ。

Zendesk 書き込みトークンを持つのはこのプロセスだけ(実運用では書込ユーザーで動かす)。
書き込み操作は (1) 内部メモ追記+タグ、(2) カスタムフィールド「担当者」セット、
(3) SPECIAL/passkey チケット限定の標準 assignee セットのみ。クローズ・公開返信は行わない。

担当割り当て: AI は category/difficulty のみ出し、担当者 ID は
コードが決定論的に決める(injection で担当を操作される経路を作らない)。
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Optional, Tuple

import common
import llm_client
import pii_mask

SUPPORT_AI_TRIAGE_TAG = os.environ.get("SUPPORT_AI_TRIAGE_TAG", "ai_triaged")
SYNC_AGENTS_ON_POST = os.environ.get("SUPPORT_AI_SYNC_AGENTS_ON_POST", "1").lower() in ("1", "true", "yes")
MAX_NOTE_LEN = 30000  # 社内メモ本文長の上限(安全側)
MIN_NOTE_LEN = 20


class ValidationError(Exception):
    pass


def normalize_record(record: dict) -> dict:
    """旧pendingレコードを新しいtriage gateスキーマへ寄せる。

    デプロイ前に生成済みのpendingをfailedへ落とさないための互換処理。
    新規生成分はgenerator側で明示的に値を持つ。
    """
    record.setdefault("answer_confidence", "medium")
    record.setdefault("severity", "not_assessed")
    record.setdefault("requires_environment_knowledge", False)
    record.setdefault("requires_runbook", False)
    record.setdefault("requires_operator_check", True)
    record.setdefault("safe_to_reply_to_user", False)
    record.setdefault("suggested_next_action", "Review the AI triage note before replying to the user.")
    return record


def resolve_assignee(
    difficulty: str, category: str, *, cursor: int, light_agents: list,
    escalation_map: dict, seed: int = 0,
) -> Tuple[Optional[int], int, str]:
    """担当者を決定論的に決める純粋関数。I/O を持たない。

    返り値: (agent_id|None, new_cursor, via)。
    - low/normal: 全体ラウンドロビン輪番から次の 1 人。new_cursor = cursor+1。via="roundrobin"
    - high:       escalation_map[category] の専門担当。輪番は進めない。via="escalation"
                  値は単一 id でも id のリストでも可。リストなら seed(=ticket_id)で
                  決定論的に 1 人選ぶ(担当者が複数いるカテゴリの分散用)。
                  対応が null/空/未定義なら輪番にフォールバック(via="escalation->roundrobin")
    agent_id は呼び出し側で allowlist 検証する(ここでは選ぶだけ)。
    """
    def pick_roundrobin(c: int) -> Tuple[Optional[int], int]:
        if not light_agents:
            return None, c
        agent = light_agents[c % len(light_agents)]
        return agent.get("id"), c + 1

    if difficulty == "high":
        spec = escalation_map.get(category)
        if isinstance(spec, list):
            spec = [s for s in spec if s]  # null 除去
            if spec:
                return spec[seed % len(spec)], cursor, "escalation"  # 輪番は進めない
        elif spec:
            return spec, cursor, "escalation"  # 単一 id。輪番は進めない
        # 専門担当未定義 → 輪番にフォールバック(この場合は輪番を消費する)
        aid, new_cursor = pick_roundrobin(cursor)
        return aid, new_cursor, "escalation->roundrobin"

    # low / normal
    aid, new_cursor = pick_roundrobin(cursor)
    return aid, new_cursor, "roundrobin"


def resolve_special_assignment(*, cursor: int, assignment_pool: dict) -> Tuple[Optional[int], Optional[int], Optional[int], int, list[str]]:
    """SPECIAL チケット用の標準担当者、group、対応担当ライトエージェントを選ぶ。

    返り値: (primary_agent_id|None, group_id|None, light_agent_id|None, new_cursor, errors)。
    SPECIAL は通常プールへフォールバックしない。名簿が不完全なら割当をスキップして
    社内メモに警告を残す。
    """
    errors: list[str] = []
    primary = assignment_pool.get("primary_agent") if isinstance(assignment_pool, dict) else None
    group = assignment_pool.get("group") if isinstance(assignment_pool, dict) else None
    light_agents = assignment_pool.get("light_agents") if isinstance(assignment_pool, dict) else None
    light_agents = light_agents or []
    primary_id = primary.get("id") if isinstance(primary, dict) else None
    group_id = group.get("id") if isinstance(group, dict) else None
    if primary_id is None:
        errors.append("SPECIAL 標準担当者が一意に同期されていません")
    if group_id is None:
        errors.append("SPECIAL グループが一意に同期されていません")
    if not light_agents:
        errors.append("SPECIAL 対応担当ライトエージェントが同期されていません")
        return primary_id, group_id, None, cursor, errors
    light_agent = light_agents[cursor % len(light_agents)]
    return primary_id, group_id, light_agent.get("id"), cursor + 1, errors


def resolve_single_agent_assignment(*, assignment_pool: dict, label: str) -> Tuple[Optional[int], list[str]]:
    """単一標準担当者 pool から assignee を選ぶ。"""
    primary = assignment_pool.get("primary_agent") if isinstance(assignment_pool, dict) else None
    primary_id = primary.get("id") if isinstance(primary, dict) else None
    errors: list[str] = []
    if primary_id is None:
        errors.append(f"{label} 標準担当者が一意に同期されていません")
    return primary_id, errors


def validate(record: dict) -> None:
    """投稿前検証。LLM 出力を無検証でシステムに流さない。"""
    if not isinstance(record.get("ticket_id"), int):
        raise ValidationError("ticket_id が int でない")
    sev = record.get("severity")
    if sev not in llm_client.SEVERITIES:
        raise ValidationError(f"不正な severity: {sev!r}")
    cat = record.get("category")
    if cat not in llm_client.CATEGORIES:
        raise ValidationError(f"不正な category: {cat!r}")
    diff = record.get("difficulty")
    if diff not in llm_client.DIFFICULTIES:
        raise ValidationError(f"不正な difficulty: {diff!r}")
    confidence = record.get("answer_confidence")
    if confidence not in llm_client.ANSWER_CONFIDENCES:
        raise ValidationError(f"不正な answer_confidence: {confidence!r}")
    for key in (
        "requires_environment_knowledge",
        "requires_runbook",
        "requires_operator_check",
        "safe_to_reply_to_user",
    ):
        if not isinstance(record.get(key), bool):
            raise ValidationError(f"{key} が bool でない")
    action = record.get("suggested_next_action")
    if not isinstance(action, str):
        raise ValidationError("suggested_next_action が str でない")
    body = record.get("note_body")
    if not isinstance(body, str) or not (MIN_NOTE_LEN <= len(body) <= MAX_NOTE_LEN):
        raise ValidationError(f"note_body の長さが不正: {len(body) if isinstance(body, str) else 'N/A'}")
    mapping = record.get("mask_mapping")
    if not isinstance(mapping, dict):
        raise ValidationError("mask_mapping が dict でない")
    # 捏造プレースホルダ(対応表に無いもの)を検出
    if pii_mask.has_unresolved_placeholders(body, mapping):
        raise ValidationError("対応表に無いプレースホルダ(捏造)が本文に残っている")


def process_one(path, *, dry_run: bool, verbose: bool = False) -> bool:
    record = normalize_record(common.read_json(path))
    ticket_id = record.get("ticket_id")
    try:
        validate(record)
    except ValidationError as e:
        common.log(f"validation NG ticket_{ticket_id}: {e}")
        common.move_to(path, "failed")
        return False

    # unmask は投稿直前にローカルで復元
    final_body = pii_mask.unmask(record["note_body"], record["mask_mapping"])
    # 復元後にプレースホルダ形式が残っていれば異常(捏造は validate 済みなので復元漏れ)
    if pii_mask.has_unresolved_placeholders(final_body, {}):
        common.log(f"unmask 漏れ ticket_{ticket_id}")
        common.move_to(path, "failed")
        return False

    # --- 担当者の決定論的決定---
    cfg = common.load_agents_config()
    support_area = str(record.get("support_area") or "").strip().lower()
    is_special = support_area == common.SPECIAL_SUPPORT_AREA
    is_passkey = support_area == common.PASSKEY_SUPPORT_AREA
    standard_assignee_id = None
    standard_group_id = None
    standard_assignee_name = None
    assignment_errors: list[str] = []
    rr_pool = "default"
    if is_special:
        rr_pool = common.SPECIAL_SUPPORT_AREA
        cursor = common.read_roundrobin(pool=rr_pool)
        pool = (cfg.get("assignment_pools") or {}).get(common.SPECIAL_SUPPORT_AREA) or {}
        standard_assignee_id, standard_group_id, agent_id, new_cursor, assignment_errors = resolve_special_assignment(
            cursor=cursor,
            assignment_pool=pool,
        )
        via = "special-roundrobin"
        if standard_assignee_id is not None:
            standard_assignee_name = common.agent_name(standard_assignee_id)
    elif is_passkey:
        cursor = common.read_roundrobin()
        pool = (cfg.get("assignment_pools") or {}).get(common.PASSKEY_SUPPORT_AREA) or {}
        standard_assignee_id, assignment_errors = resolve_single_agent_assignment(
            assignment_pool=pool,
            label="passkey",
        )
        agent_id, new_cursor, via = None, cursor, "passkey-fixed"
        if standard_assignee_id is not None:
            standard_assignee_name = common.agent_name(standard_assignee_id)
    else:
        cursor = common.read_roundrobin()
        agent_id, new_cursor, via = resolve_assignee(
            record["difficulty"], record["category"], cursor=cursor,
            light_agents=cfg.get("light_agents", []), escalation_map=cfg.get("escalation_map", {}),
            seed=int(ticket_id),
        )
    # allowlist 検証: 選ばれた ID が LIGHT_AGENTS に無ければ書き込まない
    allow = common.light_agent_ids()
    if agent_id is not None and agent_id not in allow:
        common.log(f"担当者 ID {agent_id} が allowlist 外 ticket_{ticket_id} -> failed/")
        common.move_to(path, "failed")
        return False
    assignee_name = common.agent_name(agent_id) if agent_id is not None else None
    # フィールド書込が可能か(field id 設定済み かつ 担当者が決まっている)
    can_assign = bool(cfg.get("assignee_field_id")) and agent_id is not None

    if dry_run:
        if verbose:
            who = f"{assignee_name} via {via}" if agent_id is not None else "(担当者なし: 名簿未設定)"
            note = "" if can_assign else " [フィールド書込はスキップ: assignee_field_id 未設定 or 担当者なし]"
            if is_special:
                who = f"SPECIAL standard={standard_assignee_name or '(未設定)'}, light={who}"
                if assignment_errors:
                    note += f" [SPECIAL割当警告: {'; '.join(assignment_errors)}]"
            elif is_passkey:
                who = f"passkey standard={standard_assignee_name or '(未設定)'}"
                if assignment_errors:
                    note += f" [passkey割当警告: {'; '.join(assignment_errors)}]"
            common.log(f"[dry-run] would post note to ticket_{ticket_id} "
                       f"(urgency:not_assessed/{record['category']}/{record['difficulty']}); "
                       f"would assign {who}{note}; cursor は進めない")
        return True

    # 本番(順序重要 — assignment behavior)
    if assignment_errors:
        final_body = final_body.rstrip() + "\n割当警告: " + "; ".join(assignment_errors)
    # a,b: 内部メモ + タグ
    common.post_internal_note(int(ticket_id), final_body, tags=[SUPPORT_AI_TRIAGE_TAG])
    # c: SPECIAL/passkey チケットでは標準担当者も設定する(タグ同期で一意に決まる場合のみ)
    if (is_special or is_passkey) and standard_assignee_id is not None:
        common.set_ticket_assignee(
            int(ticket_id),
            int(standard_assignee_id),
            group_id=int(standard_group_id) if is_special and standard_group_id is not None else None,
        )
    # d: カスタムフィールド「担当者」セット(可能な場合のみ)
    if can_assign:
        common.set_assignee_field(int(ticket_id), agent_id)
        # e: 書込成功後に輪番カウンタを前進(輪番を消費した場合のみ進む)
        if new_cursor != cursor:
            common.write_roundrobin(new_cursor, pool=rr_pool)
    # f: done へ
    common.move_to(path, "done")
    if verbose:
        a = f", assigned {assignee_name} via {via}" if can_assign else " (担当未設定)"
        if is_special and standard_assignee_name:
            a += f", standard assignee {standard_assignee_name}"
        if is_passkey and standard_assignee_name:
            a += f", passkey standard assignee {standard_assignee_name}"
        common.log(f"posted note + tag '{SUPPORT_AI_TRIAGE_TAG}' to ticket_{ticket_id}{a} -> done/")
    return True


def run_once(*, dry_run: bool, verbose: bool = False) -> int:
    common.ensure_spool_dirs()
    files = common.list_queue("pending", "ticket_*.json")
    if files and SYNC_AGENTS_ON_POST:
        try:
            cfg = common.sync_agents_config()
            if verbose:
                common.log(f"agents synced: {len(cfg.get('light_agents', []))} light agents")
                for err in cfg.get("assignment_pool_errors", []):
                    common.log(f"agents sync warning: {err}")
                for err in cfg.get("escalation_errors", []):
                    common.log(f"agents sync warning: {err}")
        except Exception as e:  # noqa: BLE001
            common.log(f"agents sync failed (local agents.json を使用): {e}")
    n = 0
    for f in files:
        try:
            if process_one(f, dry_run=dry_run, verbose=verbose):
                n += 1
        except Exception as e:
            common.log(f"post error ticket (item {f.name}): {e}")
            # 投稿失敗はファイルを pending に残す(リトライ容易性 — documented behavior)
    if verbose:
        common.log(f"poster done: {len(files)} pending, {n} {'validated' if dry_run else 'posted'}")
    return n


def run_forever(*, dry_run: bool, verbose: bool = False, interval: int = 30) -> None:
    common.log(f"poster start (dry_run={dry_run}, interval={interval}s)")
    while True:
        try:
            run_once(dry_run=dry_run, verbose=verbose)
        except Exception as e:
            common.log(f"poster loop error (continuing): {e}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Triage poster (process B)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="検証のみ・投稿しない")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()
    if args.once:
        run_once(dry_run=args.dry_run, verbose=args.verbose)
    else:
        run_forever(dry_run=args.dry_run, verbose=args.verbose, interval=args.interval)


if __name__ == "__main__":
    main()
