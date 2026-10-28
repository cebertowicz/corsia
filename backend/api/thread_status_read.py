"""Read the status API's thread and permitted recent output in one snapshot."""
from __future__ import annotations

from infra.conductor import get_coordination_scopes
from infra.database import get_db
from infra.queries import get_thread_for_user
from infra.thread_confinement import (
    ISOLATED_RESULT_LABEL, finished_agent_result, restrict_agent_read,
)


def read_status(thread_id: str, user: dict, limit: int):
    db = get_db()
    try:
        thread = get_thread_for_user(thread_id, user["id"], db=db)
        if not thread:
            return None
        active = db.execute(
            "SELECT id, status, started_at FROM active_turns "
            "WHERE thread_id = ? AND status='running' ORDER BY started_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        caller = user.get("thread_id") if user.get("token_kind") == "internal" else None
        restricted = restrict_agent_read(thread["confinement"], thread_id, caller)
        recent = db.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE thread_id = ? ORDER BY created_at DESC LIMIT ?",
            (thread_id, limit),
        ).fetchall() if limit and not restricted else []
        final = finished_agent_result(db, thread_id) if limit and restricted else None
        if final:
            final["content"] = f"{ISOLATED_RESULT_LABEL}\n\n{final['content']}"
        thread = dict(thread)
        if restricted:
            thread["title"] = None
        return (
            thread, dict(active) if active else None,
            get_coordination_scopes(thread_id, db=db),
            [final] if final else [dict(row) for row in reversed(recent)],
        )
    finally:
        db.close()
