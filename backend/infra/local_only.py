"""Local-only thread policy at model-admission and agent-send boundaries."""

from __future__ import annotations

import sqlite3

from llm.locality import locality_refusal
from prompts.cloud_off import project_cloud_off_text, thread_local_only_text

LOCAL_ONLY_REFUSAL_CODE = "local_only_requires_local_model"
LOCAL_ONLY_SEND_NEEDS_CONFIRM_CODE = "local_only_send_needs_confirm"


class LocalOnlyRefused(ValueError):
    """A typed model-admission refusal with stable agent-facing text."""

    code = LOCAL_ONLY_REFUSAL_CODE

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _catalog_row(model: str, *, db: sqlite3.Connection) -> dict:
    from llm.resolution import catalog_row

    return catalog_row(model, db=db) or {"litellm_model": model}


def _ready_local_selectors() -> list[str]:
    from llm.model_status import catalog_base_rows

    return sorted({
        str(status["litellm_model"])
        for status in catalog_base_rows()
        if status.get("is_local") is True
        and status.get("status") == "ready"
        and status.get("litellm_model")
    })


def local_only_model_refusal(
    model: str | None,
    *,
    thread_local_only: bool,
    project_id: str | None,
    db: sqlite3.Connection,
    spawn: bool = False,
) -> str | None:
    """Return the governing locality refusal for one model selection."""
    if not isinstance(model, str) or not model.strip():
        return None
    if not thread_local_only and not project_id:
        return None
    selector = model.strip()
    reason = locality_refusal(
        _catalog_row(selector, db=db),
        thread_local_only=thread_local_only,
        project_id=project_id,
        db=db,
    )
    if reason is None:
        return None
    selectors = _ready_local_selectors()
    if reason == "project_cloud_off":
        return project_cloud_off_text(selector, selectors, spawn=spawn)
    if not thread_local_only:
        return None
    return thread_local_only_text(selector, selectors, spawn=spawn)


def child_local_only_root(
    parent_ids: tuple[str | None, ...], *, db: sqlite3.Connection,
) -> str | None:
    """Return the root link a child stores, whether its root is Local only or not."""
    for parent_id in dict.fromkeys(parent_ids):
        if not parent_id:
            continue
        parent = db.execute(
            "SELECT COALESCE(local_only_root_thread_id,id) AS root_thread_id "
            "FROM threads WHERE id=?",
            (parent_id,),
        ).fetchone()
        if parent:
            return str(parent["root_thread_id"])
    return None


def require_local_only_model(
    model: str | None,
    *,
    thread_local_only: bool,
    project_id: str | None,
    db: sqlite3.Connection,
    spawn: bool = False,
) -> None:
    """Raise the one typed refusal for a disallowed Local-only model binding."""
    refusal = local_only_model_refusal(
        model,
        thread_local_only=thread_local_only,
        project_id=project_id,
        db=db,
        spawn=spawn,
    )
    if refusal:
        if thread_local_only:
            raise LocalOnlyRefused(refusal)
        raise ValueError(refusal)



def send_confirmation_refusal(
    sender: dict,
    recipient: dict,
    *,
    no_sensitive_content: object,
    db: sqlite3.Connection,
) -> str | None:
    """Require a local-only sender to confirm a cloud-bound recipient is safe."""
    if not sender.get("local_only"):
        return None
    # Every recipient thread is read by its own bound model, so only its
    # locality decides; a person's chat on a cloud model is still cloud-bound.
    recipient_model = recipient.get("pending_model") or recipient.get("model")
    recipient_name = str(recipient.get("title") or recipient.get("id") or "This recipient")
    if not isinstance(recipient_model, str) or not recipient_model.strip():
        if no_sensitive_content is True:
            return None
        return (
            f"{LOCAL_ONLY_SEND_NEEDS_CONFIRM_CODE}: {recipient_name} has no selected model and "
            "may adopt a cloud model. Remove anything sensitive from the message (keys, secrets, "
            "personal data, private file contents), then call again with no_sensitive_content: true."
        )
    refusal = local_only_model_refusal(
        recipient_model,
        thread_local_only=True,
        project_id=recipient.get("project_id") or None,
        db=db,
    )
    if refusal is None or no_sensitive_content is True:
        return None
    return (
        f"{LOCAL_ONLY_SEND_NEEDS_CONFIRM_CODE}: {recipient_name} runs on a cloud model "
        f"({recipient_model}). Remove anything sensitive from the message (keys, secrets, "
        "personal data, private file contents), then call again with no_sensitive_content: true."
    )


def send_confirmation_error(
    sender: dict,
    recipient: dict,
    no_sensitive_content: object,
) -> str | None:
    """Read model locality and return the send confirmation refusal, if any."""
    if not sender.get("local_only"):
        return None
    from infra.database import get_db

    db = get_db()
    try:
        return send_confirmation_refusal(
            sender, recipient, no_sensitive_content=no_sensitive_content, db=db,
        )
    finally:
        db.close()
