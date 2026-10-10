"""Browser lifecycle notices through the existing durable conversation surface."""
from __future__ import annotations

import logging
from typing import TypeAlias

from core.thread_identity import root_visible_thread_id
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.queries import insert_message
from infra.send_broadcast import broadcast_message_rows

logger = logging.getLogger(__name__)
CleanupNotice: TypeAlias = tuple[str, str]
BLOCKED_NOTICE = (
    "Browser cleanup could not be verified, so browser work in this conversation is blocked. "
    "To recover, close any leftover agent-browser process or the Chrome window it opened, "
    "then restart CORSIA; the restart clears this block. If the browser can still confirm "
    "closure, asking the agent to close the browser once also clears it. A restart does not "
    "by itself prove that old browser processes are gone."
)
RECOVERED_NOTICE = (
    "Browser cleanup is now verified. The previously reported browser cleanup blocker is resolved."
)


def _notice_audience(db, thread_id: str) -> str:
    root = root_visible_thread_id(thread_id, db=db)
    if root is None:
        raise ValueError('browser notice lineage is unavailable')
    return root


def _persist_notice(thread_id: str, recovered: CleanupNotice | None) -> CleanupNotice:
    with write_transaction(None, scope='browser_cleanup_notice') as db:
        audience = _notice_audience(db, thread_id)
        if recovered is not None:
            # A recovery belongs to the warning already seen by this same audience.
            prior = db.execute('SELECT thread_id FROM messages WHERE id=?', (recovered[1],)).fetchone()
            if audience != recovered[0] or prior is None or prior['thread_id'] != audience:
                raise ValueError('browser recovery notice audience changed')
        message_id = insert_message(
            audience, 'system', RECOVERED_NOTICE if recovered else BLOCKED_NOTICE,
            {'type': 'browser_cleanup_recovered' if recovered else 'browser_cleanup_error'}, db=db,
        )
    return audience, message_id


async def report_cleanup(thread_id: str, *, recovered: CleanupNotice | None = None) -> CleanupNotice:
    """Commit a notice for the visible owner before publishing its stored projection."""
    notice = await run_in_db(_persist_notice, thread_id, recovered)
    try:
        await broadcast_message_rows(notice[0], (notice[1],))
    except Exception:
        # The committed row remains authoritative and is visible after reconnect.
        logger.error('browser cleanup notice broadcast failed; durable notice retained')
    return notice
