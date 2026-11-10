"""Session list, creation, and system-message endpoints.

Message routes live in thread_messages.py.
Session metadata, model-selection, and deletion routes are in thread_mutations.py.
Runtime-state routes (KV-cache warm, active-turn snapshot) are in
thread_runtime.py.
"""

from __future__ import annotations

import uuid
from infra.time import utc_now_iso

from fastapi import APIRouter, HTTPException

from infra.auth import authenticate
from infra.chat_comment_answers import projected_saved_content, strip_answer_tags
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_read
from infra.packed_rows import fetch_all
from infra.product_events import emit_product_event
from infra.conductor import get_all_coordination_ids
from infra.corsia_agent import get_corsia_agent
from infra.fresh_threads import create_visible_fresh_thread
from infra.local_only import add_local_only_payload_fields
from infra.queries import (
    get_thread_for_user,
    insert_message,
    thread_available_sql,
    thread_is_explicitly_archived,
)

router = APIRouter(prefix="/api", tags=["threads"])

#: Thread columns the chat list does not carry: turn, model-switch, compaction, confinement and
#: local-only engine state that no caller of this route reads. An unread column still costs its
#: name once per listed thread. The frontend keeps each row as its `Thread`
#: (frontend/src/lib/stores/threads.ts), which declares none of these; thread detail and the
#: routes that own each value (model switch, local-only, roster) still serve them. Any other
#: `threads` column reaches the list unless it is named here.
_LIST_INTERNAL_COLUMNS = (
    "agent_roster_updated_at",
    "confinement",
    "confined_read_dirs",
    "execution_epoch",
    "history_epoch",
    "last_auto_retry_at",
    "last_context_change_seq",
    "local_only_cut_seq",
    "next_input_order",
    "next_message_seq",
    "pending_compaction_detail",
    "pending_compaction_trigger",
    "pending_execution_epoch",
    "switch_actor_id",
    "switch_actor_kind",
    "switch_after_input_order",
    "switch_checkpoint_choice",
    "switch_reason",
    "switch_requested_at",
)


@router.get("/threads")
async def list_threads(token: str, archived: int = 0):
    """Return visible chat threads for the authenticated user."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    def _query():
        db = get_db()
        conductor_ids = get_all_coordination_ids(db=db)
        # Sidebar visibility = parent presence (single source of truth):
        # ``spawned_by IS NULL`` ⇒ top-level/visible. By design ``spawned_by`` is
        # set ONLY for hidden subagents — every visible-peer spawn path leaves it
        # NULL: delegation create_thread sets it solely for thread_kind=subagent
        # (and rejects parentless subagents, #539); create_child_thread always
        # sets it, because every child it creates is a hidden subagent.
        # AP2 forbids re-introducing a semantic classification column that
        # could resurrect the old divergence.
        from infra.thread_confinement import Reader, hidden_clause, project_thread

        reader = Reader.from_user(user)
        visible, identity = hidden_clause(reader, "s")
        sql = f"""SELECT s.*,
               CASE WHEN {visible} THEN
                 CASE WHEN instr(m.content, '[c') > 0 THEN m.content ELSE substr(m.content, 1, 100) END
               END as last_message,
               CASE WHEN {visible} THEN m.id END as last_message_id
               FROM threads s LEFT JOIN messages m ON m.id =
                 (SELECT lm.id FROM messages lm WHERE lm.thread_id = s.id ORDER BY lm.created_at DESC LIMIT 1)
               WHERE s.user_id = ?
               AND {thread_available_sql('s', include_user_archived=bool(archived))}
               AND s.spawned_by IS NULL"""
        params: list = [*identity, *identity, user["id"]]
        if conductor_ids:
            placeholders = ','.join('?' * len(conductor_ids))
            sql += f" AND (s.id NOT IN ({placeholders}) OR NOT EXISTS (SELECT 1 FROM projects p WHERE p.id = s.project_id AND p.status = 'archived'))"
            params.extend(conductor_ids)
        sql += " ORDER BY s.updated_at DESC"
        try:
            rows = fetch_all(db, sql, params)
            agent_id = get_corsia_agent(db=db) if conductor_ids else None
            projected = []
            coordination = []
            for row in rows:
                item = project_thread(reader, row)
                for column in _LIST_INTERNAL_COLUMNS:
                    item.pop(column, None)
                message_id = item.pop('last_message_id', None)
                content = projected_saved_content(db, row['id'], message_id, item['last_message']) if message_id else None
                content = strip_answer_tags(content)
                item['last_message'] = content[:100] if content is not None else None
                item['archived'] = 1 if thread_is_explicitly_archived(row) else 0
                if row['id'] in conductor_ids:
                    item['coordination'] = 'corsia' if row['id'] == agent_id else 'conductor'
                    coordination.append(item)
                else:
                    projected.append(item)
            add_local_only_payload_fields([*projected, *coordination], db=db)
            return projected, coordination
        finally:
            db.close()

    threads, coordination = await run_read(_query)
    from connections.harness_refresh import schedule_refresh_for_selections

    schedule_refresh_for_selections(thread.get("model") for thread in threads)
    return {"threads": threads, "coordination": coordination}


@router.post("/threads")
async def create_thread(token: str, body: dict):
    """Create a visible fresh chat thread."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    now = utc_now_iso()
    project_id = body.get("project_id") or None
    from connections.harness_refresh import prepare_selection

    await prepare_selection(body.get("model"))

    try:
        thread_row = await create_visible_fresh_thread(
            user_id=user["id"],
            profile=body.get("profile", "personal"),
            title=body.get("title", "New Chat"),
            model=body.get("model"),
            project_id=project_id,
            reasoning_effort=body.get("reasoning_effort") or None,
            created_at=now, account_id=body.get("account_id"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    thread_id = thread_row["id"]
    emit_product_event(
        "session", "started",
        thread_id=thread_id, user_id=user["id"],
        properties={"project_id": project_id},
    )
    return {"thread_id": thread_id}


@router.post("/threads/{thread_id}/system-message")
async def add_system_message(thread_id: str, token: str, body: dict):
    """Insert a system message (e.g. model switch log) into a session."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "content required")
    msg_id = str(uuid.uuid4())
    now = utc_now_iso()

    def _insert():
        with write_transaction(None, scope="add_system_message") as db:
            thread_row = get_thread_for_user(thread_id, user["id"], db=db)
            if not thread_row:
                return False
            insert_message(
                thread_id, "system", content, None,
                msg_id=msg_id, db=db, created_at=now,
            )
            return True

    found = await run_in_db(_insert)
    if not found:
        raise HTTPException(404)
    return {"ok": True, "message_id": msg_id}
