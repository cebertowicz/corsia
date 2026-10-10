"""Who an agent live write is attributed to, for the browser typing reveal."""
from __future__ import annotations

import logging

from doceditor.agent_name import agent_display_name
from doceditor.document_scope import UI_THREAD
from doceditor.live_publication_record import WriteActor
from infra.db_async import run_in_db
from infra.queries import get_thread

log = logging.getLogger(__name__)


async def agent_author(actor: WriteActor | None, kind: str) -> dict[str, str] | None:
    """The writing agent for an agent write or edit; None for users, flushes and sync.

    The name is cosmetic, so a failed lookup falls back to the thread id rather
    than failing the write.
    """
    if actor is None or actor.direct_user or actor.thread_id == UI_THREAD or kind not in {"write", "edit"}:
        return None
    try:
        thread = await run_in_db(get_thread, actor.thread_id)
    except Exception:
        log.warning("Live write author lookup failed; revealing without a title", exc_info=True)
        thread = None
    author = {"thread_id": actor.thread_id, "name": agent_display_name(thread["title"] if thread else None, actor.thread_id)}
    if actor.call_id:
        author["call_id"] = actor.call_id
    return author
