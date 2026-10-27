"""After-response delivery adapter for a person's document comment."""

from __future__ import annotations

from doceditor import feedback_batches
from infra.database import get_db
from infra.db_async import run_in_db


async def deliver_person_comment_after_response(
    batch_ids: list[str], artifact_id: str, delivery_policy: str,
) -> None:
    """Run immediate person-comment delivery after a saved response.

    Held document and image comments remain in their policy-specific open
    batch. A failed immediate delivery leaves its batch open, so the
    continuation sweep remains the durable retry path. This is the only
    post-response seam allowed to deliver or announce a person comment.
    """
    if delivery_policy != feedback_batches.IMMEDIATE:
        return

    def deliver() -> list[str]:
        db = get_db()
        try:
            return feedback_batches.deliver_person_comment(db, batch_ids, artifact_id)
        finally:
            db.close()

    woken = await run_in_db(deliver)
    if woken:
        from plans import public

        await public.drive_threads(woken)
