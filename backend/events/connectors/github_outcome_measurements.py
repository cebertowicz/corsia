"""Optional measurement facts derived from GitHub outcome payload fields."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from events.connectors import github_outcome_input as shape

_REVERTED_SHA = re.compile(r"This reverts commit ([0-9a-f]{40})\b")


def minutes_between(start: Any, end: Any) -> int | None:
    """Whole minutes between optional GitHub timestamps, or no fact."""
    try:
        start_at = datetime.fromisoformat(shape.timestamp(start).replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(shape.timestamp(end).replace("Z", "+00:00"))
    except shape.PayloadRefusal:
        return None
    return int((end_at - start_at).total_seconds() // 60)


def reverted_sha(title: Any, body: str) -> str | None:
    """The SHA stated by GitHub's canonical revert template, if both parts match."""
    if not isinstance(title, str) or not title.startswith('Revert "'):
        return None
    match = _REVERTED_SHA.search(body)
    return match.group(1) if match is not None else None
