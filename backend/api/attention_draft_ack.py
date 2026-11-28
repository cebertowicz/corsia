"""Activity acknowledgement of an in-flight email draft."""
from fastapi import APIRouter, Depends, HTTPException, Response

from infra.auth import require_user
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db

router = APIRouter()


def _acknowledge_draft(draft_id: str, user_id: str) -> int:
    """Serialize the ownership/state check and acknowledgement with send transitions."""
    db = get_db()
    try:
        with write_transaction(db, scope='activity_draft_acknowledge'):
            row = db.execute(
                """SELECT d.state FROM action_drafts d JOIN threads t ON t.id=d.thread_id
                    WHERE d.id=? AND d.kind='email' AND t.user_id=?""",
                (draft_id, user_id),
            ).fetchone()
            if row is None:
                return 404
            if row['state'] != 'sending':
                return 409
            db.execute(
                "INSERT OR IGNORE INTO activity_draft_acknowledgements(user_id, draft_id) VALUES (?, ?)",
                (user_id, draft_id),
            )
        return 204
    finally:
        db.close()


@router.post('/drafts/{draft_id}/acknowledge', status_code=204)
async def acknowledge_draft(draft_id: str, user: dict = Depends(require_user)):
    status = await run_in_db(lambda: _acknowledge_draft(draft_id, user['id']))
    if status == 404:
        raise HTTPException(status_code=404, detail='draft not found')
    if status == 409:
        raise HTTPException(status_code=409, detail='draft is not sending')
    return Response(status_code=204)
