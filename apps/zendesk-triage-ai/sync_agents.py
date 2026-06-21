#!/usr/bin/env python3
"""Sync Zendesk light agents into agents.json."""

from __future__ import annotations

import argparse
import json

import common


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Zendesk light agents into TRIAGE_AGENTS_FILE")
    ap.add_argument("--dry-run", action="store_true", help="fetch and validate without writing agents.json")
    args = ap.parse_args()

    cfg = common.sync_agents_config(dry_run=args.dry_run)
    out = {
        "light_agent_count": len(cfg.get("light_agents", [])),
        "escalation_map": cfg.get("escalation_map", {}),
        "escalation_errors": cfg.get("escalation_errors", []),
        "dry_run": args.dry_run,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if out["escalation_errors"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
