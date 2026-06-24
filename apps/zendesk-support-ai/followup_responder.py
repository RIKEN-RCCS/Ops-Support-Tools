#!/usr/bin/env python3
"""Follow-up responder.

pending_followup/ を消費し、追加質問への返信ドラフトを内部メモとして投稿する。
公開返信は行わない。
"""

from __future__ import annotations

import argparse
import time
from typing import Any, Optional

import common
import llm_client
import pii_mask


FOLLOWUP_DRAFTED_TAG = "ai_followup_drafted"
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


def _public_conversation(comments: list[dict[str, Any]], *, limit: int = 12) -> str:
    public_comments = [c for c in comments if c.get("public") is True and _comment_body(c)]
    lines = []
    for comment in public_comments[-limit:]:
        author = comment.get("author_id")
        created = comment.get("created_at") or ""
        lines.append(f"[{created} author_id={author}]\n{_comment_body(comment)}")
    return "\n\n---\n\n".join(lines)


def _validate_result(result: dict[str, Any]) -> None:
    if not isinstance(result.get("summary"), str):
        raise ValidationError("summary が string でない")
    if not isinstance(result.get("answerable"), bool):
        raise ValidationError("answerable が bool でない")
    if not isinstance(result.get("needs_agent_review"), bool):
        raise ValidationError("needs_agent_review が bool でない")
    body = result.get("draft_reply")
    if not isinstance(body, str) or not (MIN_NOTE_LEN <= len(body) <= MAX_NOTE_LEN):
        raise ValidationError(f"draft_reply の長さが不正: {len(body) if isinstance(body, str) else 'N/A'}")
    if not isinstance(result.get("agent_note"), str):
        raise ValidationError("agent_note が string でない")


def _build_internal_note(result: dict[str, Any], *, model: str, target_comment_id: Optional[int]) -> str:
    answerable = "yes" if result.get("answerable") else "no"
    review = "yes" if result.get("needs_agent_review") else "no"
    comment_line = target_comment_id if target_comment_id is not None else "(未指定)"
    return (
        f"🤖 AI Followup 返信ドラフト(自動生成・参考情報 / model: {model})\n"
        "─────────────────────────────\n"
        f"■ 対象 comment_id: {comment_line}\n"
        f"■ この情報だけで回答可能: {answerable}　／　担当者確認推奨: {review}\n\n"
        f"■ 追加質問の要約\n{result.get('summary', '').strip()}\n\n"
        f"■ 返信ドラフト\n{result.get('draft_reply', '').strip()}\n\n"
        f"■ 担当者向け補足\n{result.get('agent_note', '').strip()}\n"
        "─────────────────────────────\n"
        "※ このメモは自動生成された返信案です。公開返信前に担当者が確認してください。"
    )


def process_one(path, verbose: bool = False) -> bool:
    event = common.read_json(path)
    ticket_id = int(event["ticket_id"])
    raw_comment_id = event.get("comment_id")
    comment_id = int(raw_comment_id) if raw_comment_id is not None else None

    try:
        ticket = common.fetch_ticket(ticket_id)
        comments = common.fetch_ticket_comments(ticket_id)
        target = _find_comment(comments, comment_id)
        if not target:
            raise RuntimeError(f"target followup comment not found: {comment_id}")

        subject = ticket.get("subject") or ""
        conversation = _public_conversation(comments)
        followup_body = _comment_body(target)
        masked, mapping = pii_mask.mask_fields([subject, conversation, followup_body])

        result = llm_client.followup_reply(masked[0], masked[1], masked[2])
        _validate_result(result)
        model = result.get("_model", "unknown")

        note = _build_internal_note(result, model=model, target_comment_id=comment_id)
        if pii_mask.has_unresolved_placeholders(note, mapping):
            raise ValidationError("対応表に無いプレースホルダ(捏造)が本文に残っている")
        final_note = pii_mask.unmask(note, mapping)
        if pii_mask.has_unresolved_placeholders(final_note, {}):
            raise ValidationError("unmask 後もプレースホルダが残っている")

        common.post_internal_note(ticket_id, final_note, tags=[FOLLOWUP_DRAFTED_TAG])
        common.move_to(path, "done")
        if verbose:
            common.log(f"followup drafted ticket_{ticket_id} comment_{comment_id} -> done/")
        return True
    except Exception as e:  # noqa: BLE001
        common.log(f"followup responder failed ticket_{ticket_id}: {e}")
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
