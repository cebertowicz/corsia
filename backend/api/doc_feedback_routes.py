"""Routes that send, retry, and surface held person-comment feedback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from doceditor import feedback_batches, feedback_state
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
            state, lanes = "open", None if delivery_policy is None else (delivery_policy,)
            if action == "retry":
                if delivery_policy is None and feedback_batches.undelivered_for_document(
                    connection,
                    author_kind="user",
                    author_id=feedback_batches.USER_AUTHOR_ID,
                    artifact_id=artifact_id,
                ):
                    raise HTTPException(422, "delivery_policy is required when retrying document feedback")
                state = "undelivered"
                lanes = None if delivery_policy is None else feedback_state.RETRY_LANES[delivery_policy]
            # What the attempt ends in is read from the batches themselves, so every trigger reports the same truth.
            batch_ids = [] if action == "retry" and delivery_policy is None else feedback_state.user_batch_ids(
                connection, state, artifact_id=artifact_id, policies=lanes,
            )
            held_items = feedback_state.pending_item_keys(connection, batch_ids)
            if action == "send":
                woken = feedback_batches.close_for_document(
                    connection,
                    author_kind="user",
                    author_id=feedback_batches.USER_AUTHOR_ID,
                    artifact_id=artifact_id,
                    reason="send",
                    delivery_policy=delivery_policy,
                )
            elif delivery_policy is not None:
                woken = feedback_batches.retry_for_document(
                    connection,
                    author_kind="user",
                    author_id=feedback_batches.USER_AUTHOR_ID,
                    artifact_id=artifact_id,
                    delivery_policy=delivery_policy,
                )
            outcome = feedback_state.outcome_counts(connection, held_items)
            undelivered = feedback_batches.undelivered_for_document(
                connection,
                author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID,
                artifact_id=artifact_id,
            )
            remaining = feedback_batches.open_counts_for_document(connection, artifact_id)
        # sent_count is what this call delivered: 0 when another sender won first or the batch ended undelivered.
        # The counts are what remains.
        return {"woken": woken, "undelivered_batches": undelivered, **outcome, **remaining}
    finally:
        db.close()


def _thread_feedback_read(user_id: str, thread_id: str) -> dict:
    """Read one thread's held-document projection without taking the writer lock."""
    db = get_db()
    try:
        if not owns_thread(thread_id, user_id, db=db):
            raise HTTPException(404, "Thread not found")
        return {"woken": [], "sent_count": 0, "undelivered_count": 0, **feedback_batches.held_for_thread(db, thread_id)}
    finally:
        db.close()


def _send_thread_feedback(user_id: str, thread_id: str) -> dict:
    """Close a thread's held-document batches; report what was delivered, what was not, and what remains."""
    db = get_db()
    try:
        if not owns_thread(thread_id, user_id, db=db):
            raise HTTPException(404, "Thread not found")
        with write_transaction(db, scope="send_thread_doc_feedback") as connection:
            batch_ids = feedback_state.user_batch_ids(
                connection, "open", recipient_id=thread_id, policies=(feedback_batches.DOCUMENT_HELD,),
            )
            held_items = feedback_state.pending_item_keys(connection, batch_ids)
            woken = feedback_batches.close_for_thread(connection, thread_id=thread_id, reason="send")
            outcome = feedback_state.outcome_counts(connection, held_items)
            feedback = feedback_batches.held_for_thread(connection, thread_id)
        return {"woken": woken, **outcome, **feedback}
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

    @router.get("/doc-feedback/thread/{thread_id}")
    async def held_user_feedback_for_thread(thread_id: str, user: dict = Depends(require_user_header_only)):
        """Return the document-held person comments routed to the current thread, one row per document."""
        return await run_in_db(_thread_feedback_read, user["id"], thread_id)

    @router.post("/doc-feedback/thread/send")
    async def send_held_user_feedback_for_thread(
        body: FeedbackThreadAction, user: dict = Depends(require_user_header_only),
    ):
        """Close and deliver every document-held person-comment lane routed to the current thread."""
        result = await run_in_db(_send_thread_feedback, user["id"], body.thread_id)
        if result["woken"]:
            from plans import public

            await public.drive_threads(result["woken"])
        return result
