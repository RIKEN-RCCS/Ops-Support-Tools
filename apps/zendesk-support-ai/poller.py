#!/usr/bin/env python3
"""取得層(spec フェーズ2)。

Search API をポーリングして新着・未処理チケットを incoming/ に積む。読み取り専用。
inbound 不要・outbound のみで完結する(spec §4)。
"""

from __future__ import annotations

import argparse
import os
import time

import common

SUPPORT_AI_TRIAGE_TAG = os.environ.get("SUPPORT_AI_TRIAGE_TAG", "ai_triaged")
SEARCH_QUERY = os.environ.get("SUPPORT_AI_TRIAGE_SEARCH_QUERY", f"type:ticket status:new -tags:{SUPPORT_AI_TRIAGE_TAG}")
POLL_INTERVAL = 180  # 秒(spec §10 初期値)


def poll_once(verbose: bool = False) -> int:
    """新着を 1 回取得し、incoming/ に積んだ件数を返す。冪等。"""
    common.ensure_spool_dirs()
    incoming = common.spool_path("incoming")

    results = common.search_tickets(SEARCH_QUERY)
    added = 0
    for t in results:
        # Search はチケット以外も返しうるので type を確認
        if t.get("result_type") not in (None, "ticket") and "id" not in t:
            continue
        tid = t.get("id")
        if tid is None:
            continue
        name = f"ticket_{tid}.json"
        target = incoming / name
        if common.queue_exists("incoming", name):
            if verbose:
                common.log(f"skip (already queued): ticket_{tid}")
            continue
        event = {
            "ticket_id": int(tid),
            "received_at": int(time.time()),
            "source": "poller",
        }
        common.atomic_write_json(target, event)
        added += 1
        if verbose:
            common.log(f"queued ticket_{tid}")
    if verbose:
        common.log(f"poll done: {len(results)} found, {added} newly queued")
    return added


def run_forever(verbose: bool = False, interval: int = POLL_INTERVAL) -> None:
    common.log(f"poller start (interval={interval}s)")
    while True:
        try:
            poll_once(verbose=verbose)
        except Exception as e:  # ループは例外を握りつぶして継続(spec §8 フェーズ2)
            common.log(f"poll error (continuing): {e}")
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Zendesk triage poller")
    ap.add_argument("--once", action="store_true", help="1 回だけ実行して終了")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL)
    args = ap.parse_args()

    if args.once:
        poll_once(verbose=args.verbose)
    else:
        run_forever(verbose=args.verbose, interval=args.interval)


if __name__ == "__main__":
    main()
