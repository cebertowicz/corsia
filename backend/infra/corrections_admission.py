"""Keep correction recording strictly outside the latency-critical send function."""

from __future__ import annotations

from typing import Any

from loguru import logger


def notify_corrections(message_id: str, *, role: str, content: str, metadata: Any, duplicate: bool) -> None:
    """Best-effort post-admission notification that never affects delivery."""
    try:
        from jobs.corrections_realtime import notify_if_owner

        notify_if_owner(message_id, role=role, content=content, metadata=metadata, duplicate=duplicate)
    except Exception:
        logger.exception("corrections classifier admission notification failed")
