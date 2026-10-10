"""The content-free product event every plan_read records."""

from __future__ import annotations

import logging
import time

from infra.product_events import emit_product_event

logger = logging.getLogger(__name__)


def emit_read(thread_id: str, read: dict, foreign: bool, size: int, started: float) -> None:
    """Record one content-free read event: whose plan, which read, its size and latency.

    Evidence only: a failure is logged and never changes what the caller gets.
    """
    mode = "node" if read.get("node_id") else read.get("mode") or "status"
    try:
        emit_product_event(
            "plans", "plan_read", thread_id=thread_id,
            properties={
                "foreign": foreign, "mode": mode, "bytes": size,
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
        )
    except Exception:  # noqa: BLE001 - evidence may never break a read
        logger.warning("plans plan_read emit failed", exc_info=True)
