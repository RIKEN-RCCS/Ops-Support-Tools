"""Followup queue idempotency tests. No Zendesk/LLM access required."""

from __future__ import annotations

import tempfile
from pathlib import Path

import common
import followup


def _use_temp_queue() -> tempfile.TemporaryDirectory[str]:
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    common.SPOOL_DIR = base / "spool"
    common.QUEUE_DB = base / "queue.sqlite"
    common.ensure_spool_dirs()
    return tmp


def test_move_to_replaces_existing_failed_item():
    tmp = _use_temp_queue()
    try:
        src = common.write_queue_item("incoming_followup", "ticket_79.json", {"ticket_id": 79, "source": "new"})
        common.write_queue_item("failed", "ticket_79.json", {"ticket_id": 79, "source": "old"})

        moved = common.move_to(src, "failed")

        assert moved.queue == "failed"
        assert not common.queue_exists("incoming_followup", "ticket_79.json")
        assert common.queue_exists("failed", "ticket_79.json")
        assert common.read_json(moved)["source"] == "new"
    finally:
        tmp.cleanup()


def test_no_followup_note_is_not_reposted_for_handled_event():
    tmp = _use_temp_queue()
    calls = []
    original = common.post_internal_note
    common.post_internal_note = lambda *args, **kwargs: calls.append((args, kwargs))
    try:
        common.write_queue_item("failed", "ticket_79.json", {"ticket_id": 79})

        followup._note_no_followup(79, reason="public requester comment not found", comment_id=None)

        assert calls == []
    finally:
        common.post_internal_note = original
        tmp.cleanup()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok:", name)
    print("OK: all followup queue tests passed")
