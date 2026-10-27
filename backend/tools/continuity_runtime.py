"""Runtime context helpers for ``continuity_edit``."""

from __future__ import annotations

from typing import Any

from infra.continuity_store import ContinuityError


def require_context(kwargs: dict[str, Any]):
    """Resolve the calling thread through the shared tool-context owner."""
    from tools.delegation import _require_context

    return _require_context(kwargs)


def next_actions_refusal(checkpoint_id: str | None) -> str:
    """Name the structured-plan home for a rejected continuity next action."""
    home = (
        "A next step belongs on the plan: use plan_update to add or change it.",
        "Name the step in progress and the exact next action in your narrative.",
    )
    return "Error: next_actions takes no new entries. " + home[bool(checkpoint_id)] + " Drop a finished one here."


def epoch(db: Any, thread_id: str) -> int:
    """Return the current history epoch or name the missing thread."""
    row = db.execute("SELECT history_epoch FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None:
        raise ContinuityError(f"thread {thread_id} does not exist")
    return int(row["history_epoch"])
