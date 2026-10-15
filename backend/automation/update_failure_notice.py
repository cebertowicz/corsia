"""Workspace-conductor notices for terminal Update failures."""

from __future__ import annotations

import asyncio
import hashlib
import json

from loguru import logger


async def tell_conductor_about_failed_update(operation_id: str) -> None:
    """Deliver one failed-Update notice and mark it only after acceptance."""
    from automation import self_restart
    from core.agent_roster_queries import workspace_conductor_contact
    from infra.database import get_db

    def contact_and_operation() -> tuple[dict | None, dict | None, str | None]:
        connection = get_db()
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

    from infra.db_async import run_in_db

    contact, operation, gap = await run_in_db(contact_and_operation)
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
        await run_in_db(self_restart.mark_conductor_told, operation_id)
    else:
        logger.warning("update failure conductor notice could not be delivered")


async def tell_conductor_about_scheduled_lapse(
    owner_id: str | None,
    summary: str,
    *,
    delivery_id: str,
) -> None:
    """Tell the workspace conductor that an owner-bound schedule did not run."""
    from core.agent_roster_queries import workspace_conductor_contact
    from infra.db_async import run_in_db

    def contact_for_owner() -> tuple[dict | None, str | None]:
        from infra.database import get_db

        connection = get_db()
        return workspace_conductor_contact(str(owner_id or ""), connection)

    contact, gap = await run_in_db(contact_for_owner)
    if contact is None:
        if gap:
            logger.warning("scheduled update conductor notice skipped: {}", gap)
        return
    from events.delivery import deliver_to_thread

    sent = await deliver_to_thread(
        str(contact["id"]),
        "system.update_schedule_lapsed",
        summary,
        delivery_id=delivery_id,
    )
    if not sent:
        logger.warning("scheduled update conductor notice could not be delivered")


async def tell_conductor_about_corrections_classifier_failure(
    version: str,
    reason: str,
    reader: str,
) -> None:
    """Tell the workspace conductor once when a pinned classifier cannot serve."""
    from infra.db_async import run_in_db
    from jobs.corrections_recording import owner_user_ids

    owner_ids = await run_in_db(owner_user_ids)
    if len(owner_ids) != 1:
        logger.warning("corrections classifier failure notice skipped: expected one owner, found {}", len(owner_ids))
        return
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
    await tell_conductor_about_scheduled_lapse(
        owner_ids[0],
        f"Corrections classifier {version} cannot run: {reader}",
        delivery_id=f"corrections-classifier-failed:{version}:{digest}",
    )


def tell_corrections_classifier_failure_detached(
    version: str,
    reason: str,
    reader: str,
    *,
    tasks: set[asyncio.Task],
) -> None:
    """Deliver a classifier failure notice without delaying correction recording."""
    task = asyncio.create_task(tell_conductor_about_corrections_classifier_failure(version, reason, reader))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_notice_failure)


def tell_conductor_detached(operation_id: str, tasks: set[asyncio.Task]) -> None:
    """Send the notice without holding the release caller open on a conductor turn."""
    task = asyncio.create_task(tell_conductor_about_failed_update(operation_id))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_notice_failure)


def tell_scheduled_lapse_detached(
    owner_id: str | None,
    summary: str,
    *,
    delivery_id: str,
    tasks: set[asyncio.Task],
) -> None:
    """Send a schedule-lapse notice without holding the nightly tick open."""
    task = asyncio.create_task(
        tell_conductor_about_scheduled_lapse(owner_id, summary, delivery_id=delivery_id)
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_notice_failure)


def _log_notice_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.opt(exception=task.exception()).error("update failure conductor notice failed")
