"""Post-turn work for WebSocket sessions.

Handles placeholder-title generation after a turn completes.
"""

from __future__ import annotations

import asyncio
from infra.time import utc_now_iso
from typing import Callable, Awaitable

from loguru import logger

from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.passive_changes import append_agent_roster_transition
from core.thread_history import generate_thread_title


DEFAULT_THREAD_TITLE = "New Chat"


async def ensure_thread_title(
    thread_id: str,
    ws_send: Callable[[dict], Awaitable[None]],
) -> None:
    """Generate a title if the session still has the placeholder title.

    A thread with no selected model keeps its placeholder: the title is made on
    the thread's own model, and the post-turn call after the first turn on a
    newly selected model names it. A failed generation also keeps the
    placeholder, so the post-turn call after the next turn tries again.
    """
    try:
        def _load_context():
            db = get_db()
            session = db.execute(
                "SELECT title, model, confinement FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            recent = db.execute(
                "SELECT role, content FROM messages WHERE thread_id = ? "
                "AND delivery_state = 'delivered' "
                "AND role IN ('user', 'assistant') AND content IS NOT NULL "
                "ORDER BY created_at ASC LIMIT 8",
                (thread_id,),
            ).fetchall()
            db.close()
            return dict(session) if session else None, [dict(r) for r in recent]

        session, recent = await run_in_db(_load_context)
        if session is None or session["confinement"] is not None:
            return
        current_title = session.get("title") or DEFAULT_THREAD_TITLE
        if current_title and current_title != DEFAULT_THREAD_TITLE:
            return
        model = str((session or {}).get("model") or "").strip()
        if not model:
            logger.info("Thread {} has no model yet; title deferred", thread_id)
            return

        title = await generate_thread_title(recent, model, thread_id=thread_id)
        if not title:
            logger.info("Thread {} has no generated title yet; retrying after its next turn", thread_id)
            return

        def _save_title():
            with write_transaction(None, scope="save_thread_title") as db:
                current = db.execute(
                    "SELECT * FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchone()
                current_title = current["title"] if current else DEFAULT_THREAD_TITLE
                if current is None or current["confinement"] is not None or (current_title and current_title != DEFAULT_THREAD_TITLE):
                    return False
                db.execute(
                    "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
                    (title, utc_now_iso(), thread_id),
                )
                if current is not None and title != current_title:
                    updated = dict(current)
                    updated["title"] = title
                    append_agent_roster_transition(
                        db,
                        before=current,
                        after=updated,
                        summary=f"Agent responsibility map updated: {title}",
                    )
                return True

        saved = await run_in_db(_save_title)
        if saved:
            await ws_send({"type": "session_updated", "thread_id": thread_id, "title": title})
    except Exception as e:
        logger.warning("Session title generation failed: {}", e)


async def start_title_early(
    thread_id: str,
    user_id: str,
) -> None:
    """Start title generation immediately after the first message is accepted.

    Called before turn dispatch so long-running turns don't leave the session
    titled "New Chat" until completion. Uses session broadcast instead of a
    live ws_send so it works whether or not the originating socket is still open.

    The double-check inside ensure_thread_title prevents a duplicate DB write
    if the post-turn call also fires (it will see the title is already set).
    """
    from infra import state

    async def _broadcast(msg: dict) -> None:
        state.broadcast_event(thread_id, user_id, msg)

    await ensure_thread_title(thread_id, _broadcast)


async def run_post_turn_tasks(
    thread_id: str,
    content: str,
    ws_send: Callable[[dict], Awaitable[None]],
) -> None:
    """Replace a placeholder title after a turn completes successfully."""
    asyncio.create_task(ensure_thread_title(thread_id, ws_send))
