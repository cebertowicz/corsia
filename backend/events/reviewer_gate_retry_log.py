"""Reporting for the reviewer gate's delayed mergeability retries."""

from __future__ import annotations

import asyncio

from loguru import logger as log


def log_retry_failure(pr_number: int, head_sha: str, task: asyncio.Task[None]) -> None:
    """Retrieve and identify any delayed retry failure after task completion."""
    try:
        task.result()
    except asyncio.CancelledError:
        log.info("reviewer-gate: delayed retry cancelled for PR #{} sha={}", pr_number, head_sha[:8])
    except Exception:
        log.exception("reviewer-gate: delayed retry raised for PR #{} sha={}", pr_number, head_sha[:8])
