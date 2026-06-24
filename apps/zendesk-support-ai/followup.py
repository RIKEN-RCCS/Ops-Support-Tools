#!/usr/bin/env python3
"""Follow-up gate.

incoming_followup/ を消費し、追加質問らしいエンドユーザー公開コメントが
見つかれば pending_followup/ へ回す。見つからなければ内部メモで誤発火を知らせる。
"""

from __future__ import annotations

import argparse
import re
import time
from typing import Any, Optional

import common


FOLLOWUP_SKIP_TAG = "ai_followup_skipped"


QUESTION_HINTS = (
    "?",
    "？",
    "でしょうか",
    "ですか",
    "ますか",
    "できますか",
    "可能ですか",
    "教えて",
    "確認",
    "どう",
    "なぜ",
    "いつ",
    "どこ",
    "どれ",
    "どの",
    "方法",
    "お願い",
    "お願いします",
    "what",
    "why",
    "when",
    "where",
    "which",
    "who",
    "how",
    "can you",
    "could you",
    "would you",
    "please",
    "help",
    "confirm",
    "check",
    "tell me",
    "let me know",
    "is it",
    "are there",
    "do i",
    "does it",
    "should i",
    "may i",
)


def _comment_body(comment: dict[str, Any]) -> str:
    return str(comment.get("plain_body") or comment.get("body") or "").strip()


def _looks_like_followup_question(body: str) -> bool:
    text = " ".join(body.split())
    text_lower = text.lower()
    if not text:
        return False
    if any(hint in text_lower for hint in QUESTION_HINTS):
        return True
    # Short imperative/request fragments are often follow-up asks even without a question mark.
    return bool(re.search(
        r"(できない|動かない|失敗|エラー|見られない|入れない|使えない|"
        r"cannot|can't|failed|failure|error|unable|does not work|not working|"
        r"permission denied|timeout|stuck|blocked)",
        text_lower,
    ))


def _pick_followup_comment(ticket: dict[str, Any], comments: list[dict[str, Any]], comment_id: Optional[int]) -> Optional[dict[str, Any]]:
    requester_id = ticket.get("requester_id")
    if comment_id is not None:
        for comment in comments:
            try:
                if int(comment.get("id")) == int(comment_id):
                    return comment
            except (TypeError, ValueError):
                continue
        return None

    public_requester_comments = [
        comment
        for comment in comments
        if comment.get("public") is True
        and requester_id is not None
        and comment.get("author_id") == requester_id
    ]
    if public_requester_comments:
        return public_requester_comments[-1]
    return None


def _note_no_followup(ticket_id: int, *, reason: str, comment_id: Optional[int]) -> None:
    detail = f"comment_id={comment_id}" if comment_id is not None else "comment_id=(未指定)"
    body = (
        "Followup webhook が発火しましたが、追加質問らしいエンドユーザー公開コメントを確認できませんでした。\n\n"
        f"- ticket_id: {ticket_id}\n"
        f"- {detail}\n"
        f"- reason: {reason}\n\n"
        "Zendesk trigger 条件が広すぎる可能性があります。条件に「コメント = パブリック」や"
        "「現在のユーザー = エンドユーザー」が入っているか確認してください。"
    )
    common.post_internal_note(ticket_id, body, tags=[FOLLOWUP_SKIP_TAG])


def process_one(path, verbose: bool = False) -> bool:
    event = common.read_json(path)
    ticket_id = int(event["ticket_id"])
    raw_comment_id = event.get("comment_id")
    comment_id = int(raw_comment_id) if raw_comment_id is not None else None

    try:
        ticket = common.fetch_ticket(ticket_id)
        comments = common.fetch_ticket_comments(ticket_id)
        comment = _pick_followup_comment(ticket, comments, comment_id)
        if not comment:
            _note_no_followup(ticket_id, reason="public requester comment not found", comment_id=comment_id)
            common.move_to(path, "done")
            return False

        body = _comment_body(comment)
        if not _looks_like_followup_question(body):
            _note_no_followup(ticket_id, reason="latest public requester comment does not look like a question", comment_id=comment_id)
            common.move_to(path, "done")
            return False

        event["comment_id"] = int(comment.get("id")) if comment.get("id") is not None else comment_id
        event["accepted_at"] = int(time.time())
        event["job"] = "followup"
        target_name = f"ticket_{ticket_id}"
        if event["comment_id"] is not None:
            target_name += f"_comment_{event['comment_id']}"
        common.atomic_write_json(common.spool_path("pending_followup") / f"{target_name}.json", event)
        path.unlink()
        if verbose:
            common.log(f"followup accepted ticket_{ticket_id} -> pending_followup/")
        return True
    except Exception as e:  # noqa: BLE001
        common.log(f"followup gate failed ticket_{ticket_id}: {e}")
        try:
            common.move_to(path, "failed")
        except OSError:
            pass
        return False


def run_once(verbose: bool = False) -> int:
    common.ensure_spool_dirs()
    files = common.list_queue("incoming_followup", "ticket_*.json")
    n = 0
    for path in files:
        if process_one(path, verbose=verbose):
            n += 1
    if verbose:
        common.log(f"followup done: {len(files)} consumed, {n} accepted")
    return n


def run_forever(verbose: bool = False, interval: int = 30) -> None:
    common.log(f"followup start (interval={interval}s)")
    while True:
        try:
            run_once(verbose=verbose)
        except Exception as e:  # noqa: BLE001
            common.log(f"followup loop error (continuing): {e}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Follow-up gate")
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
