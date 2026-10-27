"""Optional measurement facts derived from GitHub outcome payload fields."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from events.connectors import github_outcome_input as shape

_REVERTED_SHA = re.compile(r"This reverts commit ([0-9a-f]{40})\b")
_REVERTED_PR = re.compile(r"Reverts ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)\b")


def _span_seconds(start: Any, end: Any) -> float | None:
    if start is None or end is None:
        return None
    start_at = datetime.fromisoformat(shape.timestamp(start).replace("Z", "+00:00"))
    end_at = datetime.fromisoformat(shape.timestamp(end).replace("Z", "+00:00"))
    return (end_at - start_at).total_seconds()


def minutes_between(start: Any, end: Any) -> int | None:
    """Whole minutes between optional GitHub timestamps, or no fact."""
    span = _span_seconds(start, end)
    if span is None:
        return None
    minutes = int(span // 60)
    return minutes if minutes >= 0 else None


def seconds_between(start: Any, end: Any) -> int | None:
    """Whole seconds between optional GitHub timestamps; no fact when one is absent or end < start."""
    span = _span_seconds(start, end)
    return int(span) if span is not None and span >= 0 else None


def reverted_sha(title: Any, body: str) -> str | None:
    """The SHA stated by a local ``git revert`` commit in a revert-titled PR."""
    if not isinstance(title, str) or not title.startswith('Revert "'):
        return None
    match = _REVERTED_SHA.search(body)
    return match.group(1) if match is not None else None


def reverted_pr_number(title: Any, body: str, repo: str) -> str | None:
    """The PR number stated by GitHub's Revert-button template for this repository."""
    if not isinstance(title, str) or not title.startswith('Revert "'):
        return None
    match = _REVERTED_PR.search(body)
    if match is None or match.group(1).lower() != repo.lower():
        return None
    try:
        return str(shape.reference_number(match.group(2)))
    except shape.PayloadRefusal:
        return None
