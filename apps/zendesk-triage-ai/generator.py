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


TRIAGE_TAG = os.environ.get("TRIAGE_TAG", "ai_triaged")


def _extract_texts(ticket_id: int):
    """subject と コメント body のみを allowlist 抽出する(spec §5)。

    author_id / via / metadata / 添付等は捨てる。Comments API を使う。
    戻り値: (subject, bodies, already_triaged)。
    """
    ticket = common.fetch_ticket(ticket_id)
    # Search インデックスの遅延で投稿済みチケットが再取得されることがある。
    # ライブのタグを確認し、二重投稿を防ぐ(spec §10 Search API の重複対策)。
    if TRIAGE_TAG in (ticket.get("tags") or []):
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


def _build_note_body(triage: dict, model: str) -> str:
    """トリアージ結果から社内メモ本文を組み立てる(プレースホルダは残したまま)。"""
    qs = triage.get("clarifying_questions") or []
    q_lines = "\n".join(f"- {q}" for q in qs) if qs else "- (なし)"
    return (
        f"🤖 AI 一次トリアージ(自動生成・参考情報 / model: {model})\n"
        "─────────────────────────────\n"
        f"■ 緊急度: {triage.get('severity')}　／　カテゴリ: {triage.get('category')}"
        f"　／　難易度: {triage.get('difficulty')}\n\n"
        f"■ 要約\n{triage.get('summary', '').strip()}\n\n"
        f"■ 推定原因\n{triage.get('probable_cause', '').strip()}\n\n"
        f"■ ユーザーへの追加確認事項\n{q_lines}\n\n"
        f"■ 一次返信ドラフト\n{triage.get('draft_reply', '').strip()}\n"
        "─────────────────────────────\n"
        "※ 担当はシステムが決定し、投稿時にこのメモ末尾へ追記されます。\n"
        f"※ このメモは {model} により自動生成されました。"
        "severity / 難易度は参考情報です。アサイン・優先度・返信は人間が判断してください。"
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
                common.log(f"skip ticket_{ticket_id}: already tagged '{TRIAGE_TAG}'")
            path.unlink()
            return False
        # subject と 全 body を一貫マッピングでマスク
        fields = [subject] + bodies
        masked, mapping = pii_mask.mask_fields(fields)
        masked_subject = masked[0]
        masked_body = "\n\n---\n\n".join(masked[1:]) if len(masked) > 1 else ""

        triage = llm_client.triage(masked_subject, masked_body)
        model = triage.get("_model", "unknown")

        record = {
            "ticket_id": ticket_id,
            "generated_at": int(time.time()),
            "model": model,
            "severity": triage["severity"],
            "category": triage["category"],
            "difficulty": triage["difficulty"],  # poster の割り当てロジックで使う
            "note_body": _build_note_body(triage, model),
            "mask_mapping": mapping,  # ローカル保持のみ。LLM にも外部にも送らない
        }
        common.atomic_write_json(common.spool_path("pending") / f"ticket_{ticket_id}.json", record)
        # 処理済み incoming はアーカイブ(done ではなく削除でもよいが監査のため failed と区別)
        path.unlink()
        if verbose:
            common.log(f"-> pending/ticket_{ticket_id}.json "
                       f"({record['severity']}/{record['category']} via {model})")
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
    incoming = common.spool_path("incoming")
    files = sorted(incoming.glob("ticket_*.json"))
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
