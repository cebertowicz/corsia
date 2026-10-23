"""After-response delivery adapter for a person's document comment."""

from __future__ import annotations

from doceditor import feedback_batches
from infra.database import get_db
from infra.db_async import run_in_db


async def deliver_person_comment_after_response(batch_ids: list[str]) -> None:
    """Run the existing close/deliver writer after a comment POST response.

    A failed delivery leaves its batch open, so the continuation sweep remains
    the durable retry path. Successful delivery emits the comment's single
    change frame after its batch has closed.
    """
    def deliver() -> list[str]:
        db = get_db()
        try:
            return feedback_batches.deliver_person_comment(db, batch_ids)
        finally:
            db.close()

    woken = await run_in_db(deliver)
    if woken:
        from plans import public

        await public.drive_threads(woken)
