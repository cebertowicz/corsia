"""Workspace-conductor notices for terminal Update failures."""

from __future__ import annotations

import asyncio
import json

from loguru import logger


async def tell_conductor_about_failed_update(operation_id: str) -> None:
    """Deliver one failed-Update notice and mark it only after acceptance."""
    from automation import self_restart
    from core.agent_roster_queries import workspace_conductor_contact
    from infra.database import get_db

    def contact_and_operation() -> tuple[dict | None, dict | None, str | None]:
        connection = get_db()
        try:
            operation = self_restart.operation_by_id(operation_id, db=connection)
            if (
                operation is None or operation.get("kind") != "update"
                or operation.get("status") != "failed" or operation.get("conductor_told_at")
            ):
                return None, operation, None
            contact, gap = workspace_conductor_contact(
                str(operation.get("requester_user") or ""), connection
            )
            return contact, operation, gap
        finally:
            connection.close()

    contact, operation, gap = await asyncio.to_thread(contact_and_operation)
    if contact is None:
        if operation is not None and gap:
            logger.warning("update failure conductor notice skipped: {}", gap)
        return
    if operation is None:
        return
    try:
        detail = json.loads(operation.get("failure_detail") or "{}")
    except (TypeError, json.JSONDecodeError):
        detail = {}
    numbers = ", ".join(
        f"{name}={detail[name]}" for name in ("needed_bytes", "free_bytes") if name in detail
    ) or "no disk measurements"
    from events.delivery import deliver_to_thread

    sent = await deliver_to_thread(
        str(contact["id"]), "system.update_failed",
        f"Update failed: {operation.get('failure_code') or 'unknown'}; {numbers}; "
        f"log: {detail.get('log_path', 'no log path')}",
        {"operation_id": operation_id, "failure_code": operation.get("failure_code")},
        delivery_id=f"update-failed:{operation_id}",
    )
    if sent:
        await asyncio.to_thread(self_restart.mark_conductor_told, operation_id)
    else:
        logger.warning("update failure conductor notice could not be delivered")
