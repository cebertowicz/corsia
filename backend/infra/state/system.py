"""System-level state — drain mode and background task handles."""

from __future__ import annotations

import asyncio

__all__ = [
    "_drain",
    "_github_ownership_login_task",
    "_library_refresh_task",
    "_mail_poller_task",
    "_nightly_job_task",
]

# Drain mode blocks automated processes and lets the frontend show restart state.
_drain: dict | None = None  # { "reason": str, "started_at": float }

_github_ownership_login_task: asyncio.Task | None = None
_library_refresh_task: asyncio.Task | None = None
_mail_poller_task: asyncio.Task | None = None
_nightly_job_task: asyncio.Task | None = None  # nightly fact extraction
