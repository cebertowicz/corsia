"""Routes that send, retry, and surface held person-comment feedback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from doceditor import feedback_batches
from infra.auth import owns_thread, require_user_header_only
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db


class FeedbackDocumentAction(BaseModel):
    artifact_id: str
    delivery_policy: Literal["immediate", "document-held", "image-held"] | None = None


class FeedbackTouch(BaseModel):
    artifact_id: str
    delivery_policy: Literal["document-held", "image-held"]


class FeedbackThreadAction(BaseModel):
    thread_id: str


# Which open count a lane-scoped send closes; an unscoped send closes every lane.
_SENT_COUNT_KEY: dict[str | None, str] = {"document-held": "document_held_count", "image-held": "image_held_count"}


def _user_feedback_write(
    assert_owns_artifact: Callable[[str, str, object], None],
    user_id: str,
    artifact_id: str,
    action: str,
    delivery_policy: str | None = None,
) -> dict:
    db = get_db()
    try:
        assert_owns_artifact(artifact_id, user_id, db)
        with write_transaction(db, scope=f"user_doc_feedback_{action}") as connection:
            woken: list[str] = []
            sent_count = 0
            if action == "send":
                before = feedback_batches.open_counts_for_document(connection, artifact_id)
                sent_count = before[_SENT_COUNT_KEY.get(delivery_policy, "open_count")]
                woken = feedback_batches.close_for_document(
                    connection,
                    author_kind="user",
                    author_id=feedback_batches.USER_AUTHOR_ID,
                    artifact_id=artifact_id,
                    reason="send",
                    delivery_policy=delivery_policy,
                )
            elif action == "retry":
                pending = feedback_batches.undelivered_for_document(
                    connection,
                    author_kind="user",
                    author_id=feedback_batches.USER_AUTHOR_ID,
                    artifact_id=artifact_id,
                )
                if delivery_policy is None and pending:
                    raise HTTPException(422, "delivery_policy is required when retrying document feedback")
                if delivery_policy is not None:
                    woken = feedback_batches.retry_for_document(
                        connection,
                        author_kind="user",
                        author_id=feedback_batches.USER_AUTHOR_ID,
                        artifact_id=artifact_id,
                        delivery_policy=delivery_policy,
                    )
            undelivered = feedback_batches.undelivered_for_document(
                connection,
                author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID,
                artifact_id=artifact_id,
            )
            remaining = feedback_batches.open_counts_for_document(connection, artifact_id)
        # sent_count is what this call closed (0 when another sender won first); the counts are what remains.
        return {"woken": woken, "undelivered_batches": undelivered, "sent_count": sent_count, **remaining}
    finally:
        db.close()


def add_feedback_routes(router, assert_owns_artifact: Callable[[str, str, object], None]) -> None:
    """Attach held-feedback routes to the comment API's ownership-scoped router."""

    async def user_feedback_action(user_id: str, body: FeedbackDocumentAction, action: str) -> dict:
        result = await run_in_db(
            _user_feedback_write, assert_owns_artifact, user_id, body.artifact_id, action, body.delivery_policy,
        )
        if result["woken"]:
            from plans import public

            await public.drive_threads(result["woken"])
        return result

    @router.post("/doc-feedback/send")
    async def send_user_feedback(
        body: FeedbackDocumentAction, user: dict = Depends(require_user_header_only),
    ):
        """Close and deliver the user's open batches for one document."""
        return await user_feedback_action(user["id"], body, "send")

    @router.post("/doc-feedback/retry")
    async def retry_user_feedback(
        body: FeedbackDocumentAction, user: dict = Depends(require_user_header_only),
    ):
        """Retry the user's undelivered batches for one document."""
        return await user_feedback_action(user["id"], body, "retry")

    @router.post("/doc-feedback/touch")
    async def touch_user_feedback(
        body: FeedbackTouch, user: dict = Depends(require_user_header_only),
    ):
        """Extend a visible held lane; the client throttles document and image touches."""

        def touch() -> dict:
            db = get_db()
            try:
                assert_owns_artifact(body.artifact_id, user["id"], db)
                with write_transaction(db, scope="touch_user_doc_feedback") as connection:
                    changed = feedback_batches.touch_for_document(
                        connection,
                        artifact_id=body.artifact_id,
                        delivery_policy=body.delivery_policy,
                    )
                return {"touched": changed}
            finally:
                db.close()

        return await run_in_db(touch)

    def thread_feedback_write(user_id: str, thread_id: str, send: bool) -> dict:
        db = get_db()
        try:
            if not owns_thread(thread_id, user_id, db=db):
                raise HTTPException(404, "Thread not found")
            with write_transaction(db, scope="send_thread_doc_feedback") as connection:
                sent_count = feedback_batches.held_for_thread(connection, thread_id)["open_count"] if send else 0
                woken = feedback_batches.close_for_thread(connection, thread_id=thread_id, reason="send") if send else []
                feedback = feedback_batches.held_for_thread(connection, thread_id)
            return {"woken": woken, "sent_count": sent_count, **feedback}
        finally:
            db.close()

    @router.get("/doc-feedback/thread/{thread_id}")
    async def held_user_feedback_for_thread(thread_id: str, user: dict = Depends(require_user_header_only)):
        """Return the document-held person comments routed to the current thread, one row per document."""
        return await run_in_db(thread_feedback_write, user["id"], thread_id, False)

    @router.post("/doc-feedback/thread/send")
    async def send_held_user_feedback_for_thread(
        body: FeedbackThreadAction, user: dict = Depends(require_user_header_only),
    ):
        """Close and deliver every document-held person-comment lane routed to the current thread."""
        result = await run_in_db(thread_feedback_write, user["id"], body.thread_id, True)
        if result["woken"]:
            from plans import public

            await public.drive_threads(result["woken"])
        return result
