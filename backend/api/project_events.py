"""The nudge a created project sends to every open tab of its owner."""

from __future__ import annotations

from loguru import logger

PROJECTS_CHANGED = "projects_changed"


def notify_projects_changed(user_id: str) -> None:
    """Best-effort after-commit nudge; the next reload recovers a miss.

    A tab's loaded project list cannot see a project created by a conductor
    tool or from another tab, so each creation path calls this once its row is
    committed.
    """
    from infra import state

    try:
        state.broadcast_to_user(user_id, {"type": PROJECTS_CHANGED})
    except Exception as exc:  # noqa: BLE001 - the committed row stays authoritative
        logger.warning("projects_changed delivery failed: {}", type(exc).__name__)
