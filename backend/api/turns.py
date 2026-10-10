"""Active turn monitoring routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from infra.auth import authenticate
from infra.database import get_db
from infra.db_async import run_in_db

router = APIRouter(prefix="/api", tags=["turns"])

_TURN_SELECT = (
    "SELECT t.id,t.thread_id,t.model,"
    "(SELECT substr(m.content,1,200) FROM messages m "
    "WHERE m.active_turn_id=t.id AND m.role IN ('user','system') "
    "ORDER BY m.input_order LIMIT 1) AS user_content_preview,"
    "t.status,t.started_at,t.ended_at,t.error "
)


@router.get("/turns/active")
async def get_active_turns(token: str):
    """Return currently active (in-flight) turns."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _query():
        db = get_db()
        rows = db.execute(
            _TURN_SELECT +
            "FROM active_turns t JOIN threads s ON s.id = t.thread_id "
            "WHERE t.status='running' AND s.user_id = ? "
            "ORDER BY t.started_at DESC",
            (user["id"],),
        ).fetchall()
        db.close()

        turns = []
        for r in rows:
            turns.append({
                "id": r[0], "thread_id": r[1], "model": r[2],
                "user_content_preview": r[3], "status": r[4], "started_at": r[5],
                "ended_at": r[6], "error": r[7],
            })
        return turns

    turns = await run_in_db(_query)
    return {"active_turns": turns, "count": len(turns)}
