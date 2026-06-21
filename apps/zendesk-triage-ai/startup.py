#!/usr/bin/env python3
"""Container startup preflight, then exec the requested service."""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, Optional

import common
import llm_client


def _enabled(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def _retry(label: str, fn: Callable[[], object], *, retries: int, delay: float) -> object:
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            result = fn()
            common.log(f"startup ok: {label}")
            return result
        except Exception as e:  # noqa: BLE001
            last_err = e
            common.log(f"startup failed: {label} attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError(f"{label} failed after {retries} attempts: {last_err}")


def preflight() -> None:
    if not _enabled("TRIAGE_STARTUP_CHECKS", "1"):
        common.log("startup checks disabled")
        return

    retries = int(os.environ.get("TRIAGE_STARTUP_RETRIES", "3"))
    delay = float(os.environ.get("TRIAGE_STARTUP_RETRY_DELAY", "5"))

    common.ensure_spool_dirs()
    common.log(f"startup ok: spool ready at {common.SPOOL_DIR}")

    agents_file = common.ensure_agents_file()
    common.log(f"startup ok: agents config ready at {agents_file}")

    user = _retry("zendesk", common.zendesk_healthcheck, retries=retries, delay=delay)
    if isinstance(user, dict):
        common.log(f"startup zendesk user: id={user.get('id')} role={user.get('role')} role_type={user.get('role_type')}")

    _retry("llm", llm_client.healthcheck, retries=retries, delay=delay)

    if _enabled("TRIAGE_SYNC_AGENTS_ON_STARTUP", "1"):
        cfg = _retry("agents sync", common.sync_agents_config, retries=retries, delay=delay)
        if isinstance(cfg, dict):
            common.log(f"startup agents synced: {len(cfg.get('light_agents', []))} light agents")
            for err in cfg.get("escalation_errors", []):
                common.log(f"startup agents warning: {err}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: startup.py COMMAND [ARG...]")
    preflight()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
