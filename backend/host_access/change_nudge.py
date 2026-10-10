"""The ``host_access_changed`` nudge, naming the threads whose chat rows it moves."""

from __future__ import annotations

from typing import Any

from loguru import logger

from core.thread_identity import root_visible_thread_ids


def changed_message(lease: dict[str, Any]) -> dict[str, Any]:
    """The nudge for a lease change.

    The chat list shows an access request on its root's visible thread, so the
    nudge names the lease's root and that visible thread; a client re-reads only
    those rows. When the visible thread cannot be resolved the nudge names no
    threads, and a client re-reads every row it lists.
    """
    root = lease["root_thread_id"]
    try:
        visible = root_visible_thread_ids([root]).get(root)
    except Exception as exc:  # noqa: BLE001 - an unnamed nudge refreshes every row
        logger.warning("host-access nudge: no visible thread for {}: {}", lease["id"], exc)
        return {"type": "host_access_changed", "lease_id": lease["id"]}
    return {
        "type": "host_access_changed",
        "lease_id": lease["id"],
        "thread_ids": [thread_id for thread_id in dict.fromkeys((root, visible)) if thread_id],
    }
