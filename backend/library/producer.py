"""Daily, read-only notice producer for installed library skills."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from events.connectors.library import (
    INSTALLED_SKILL_TYPE,
    ITEM_CHANGED_EVENT,
    ITEM_WITHDRAWN_EVENT,
    LIBRARY_CONNECTOR,
    LIBRARY_INTEGRATION,
)
from library.installed import local_edits, read_origin
from library.items import item_manifest, source_ref


log = logging.getLogger(__name__)
REFRESH_SECONDS = 24 * 60 * 60

INSTALLED_ROWS_SQL = """SELECT identities.external_id, identities.path, registrations.parent_thread_id
    FROM artifact_identities AS identities
    JOIN artifact_registrations AS registrations ON registrations.artifact_id=identities.id
    WHERE identities.integration=? AND identities.type=? AND identities.state='active'
      AND registrations.relationship_state IN ('owner-active', 'owner-pinned')"""


def _installed_rows(db=None) -> list[dict[str, Any]]:
    """Read only active installed-library artifacts, never scan the full registry."""
    from infra.database import get_db

    owned = db is None
    connection = db or get_db()
    try:
        rows = connection.execute(
            INSTALLED_ROWS_SQL, (LIBRARY_INTEGRATION, INSTALLED_SKILL_TYPE),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owned:
            connection.close()


def produce_once() -> int:
    """Compare one cached/raw index read with active installed artifacts and deliver notices."""
    from events.platform import deliver_platform_event_sync
    from library import load_index

    index = load_index()  # Exactly one cache-backed raw-main read per producer refresh.
    items = {item["name"]: item for item in index.skills}
    delivered = 0
    for row in _installed_rows():
        payload = _notice_payload(row, items)
        if payload is None:
            continue
        deliveries = deliver_platform_event_sync(LIBRARY_CONNECTOR, payload)
        from events.platform import DUPLICATE

        delivered += sum(delivery.outcome != DUPLICATE for delivery in deliveries)
    return delivered


def _notice_payload(row: dict[str, Any], items: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    path = row.get("path")
    external_id = row.get("external_id")
    if not isinstance(path, str) or not isinstance(external_id, str):
        return None
    name = external_id.rpartition("/")[2]
    item = items.get(name)
    origin = read_origin(Path(path).parent)
    if item is None or origin is None or not isinstance(origin.get("source"), str):
        return None
    edited, changed_files = local_edits(Path(path).parent, origin)
    base = {
        "external_id": external_id,
        "name": name,
        "installed_version": origin.get("version"),
        "library_version": item["version"],
        "changed_files": changed_files,
        "edited": edited,
        "withdrawn_reason": item["withdrawn_reason"],
    }
    if item["withdrawn"]:
        return {"event_type": ITEM_WITHDRAWN_EVENT, **base}
    if origin["source"] != source_ref(item) or origin.get("item_manifest") != item_manifest(item):
        return {"event_type": ITEM_CHANGED_EVENT, **base}
    return None


async def library_refresh_loop() -> None:
    """Run an initial check then one read-only producer pass per day."""
    while True:
        try:
            await asyncio.to_thread(produce_once)
        except Exception:
            log.exception("library notice refresh failed")
        await asyncio.sleep(REFRESH_SECONDS)


def request_refresh() -> None:
    """Schedule one producer pass after an agent refreshes the library index."""
    import infra.app_loop as app_loop

    async def refresh() -> None:
        try:
            await asyncio.to_thread(produce_once)
        except Exception:
            log.exception("library notice refresh failed")

    try:
        app_loop.submit(refresh())
    except app_loop.AppLoopUnavailable:
        # Calls before startup and after shutdown have no live recipient; the
        # daily startup loop remains the recovery path.
        return
