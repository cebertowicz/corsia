"""Keep a cloud-off project's tool-result images on this Mac."""

from __future__ import annotations

from typing import Any

from loguru import logger


def media_locality_refusal_text(delivery: Any, model: str | None) -> str | None:
    """Return the agent-facing refusal when ``delivery``'s thread may not send an image to ``model``.

    A persisted tool-result row carries only its message id, so the thread comes from the
    canonical message row; a row with no thread has no project to enforce.
    """
    message_id = getattr(delivery, "message_id", None)
    if not message_id or not model:
        return None
    from infra.database import get_db
    from llm.locality import thread_locality_refusal
    from prompts.cloud_off import project_cloud_off_text

    db = get_db()
    try:
        row = db.execute("SELECT thread_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        thread_id = str(row["thread_id"]) if row else None
        refusal = thread_locality_refusal(model, thread_id=thread_id, db=db) if thread_id else None
    finally:
        db.close()
    if not refusal:
        return None
    logger.info("media delivery refused: thread={} model={} reason={}", thread_id, model, refusal)
    return project_cloud_off_text(model, _ready_local_models())


def _ready_local_models() -> list[str]:
    """Catalog ids of local models that can run now, for the refusal's recovery text."""
    from llm.model_status import get_model_status_rows

    return [
        str(row["id"]) for row in get_model_status_rows()
        if row.get("is_local") and row.get("status") == "ready"
    ]
