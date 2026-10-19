"""Local-only thread policy at model-admission and agent-send boundaries."""

from __future__ import annotations

import sqlite3

from llm.locality import locality_refusal

LOCAL_ONLY_REFUSAL_CODE = "local_only_requires_local_model"


def _catalog_row(model: str, *, db: sqlite3.Connection) -> dict:
    from llm.resolution import catalog_row

    return catalog_row(model, db=db) or {"litellm_model": model}


def _ready_local_selectors(*, project_id: str | None, db: sqlite3.Connection) -> str:
    from llm.model_status import catalog_base_rows

    selectors: list[str] = []
    for status in catalog_base_rows():
        selector = str(status.get("litellm_model") or "")
        if not selector or status.get("status") != "ready":
            continue
        if locality_refusal(
            _catalog_row(selector, db=db), thread_local_only=True, project_id=project_id,
        ) is None:
            selectors.append(selector)
    return ", ".join(sorted(set(selectors))) or "no ready local models"


def local_only_model_refusal(
    model: str | None,
    *,
    thread_local_only: bool,
    project_id: str | None,
    db: sqlite3.Connection,
) -> str | None:
    """Return the one recoverable refusal for a non-local model selection."""
    if not thread_local_only or not isinstance(model, str) or not model.strip():
        return None
    reason = locality_refusal(
        _catalog_row(model.strip(), db=db),
        thread_local_only=True,
        project_id=project_id,
    )
    if reason is None:
        return None
    selectors = _ready_local_selectors(project_id=project_id, db=db)
    return (
        f"{LOCAL_ONLY_REFUSAL_CODE}: This Local only thread can run only local "
        f"models because {reason}. Choose one of: {selectors}."
    )


def thread_model_refusal(row: sqlite3.Row, model: str | None, *, db: sqlite3.Connection) -> str | None:
    """Apply a persisted thread's local-only scope to one model selection."""
    return local_only_model_refusal(
        model,
        thread_local_only=bool(row["local_only"]),
        project_id=row["project_id"] or None,
        db=db,
    )


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
    recipient_model = recipient.get("model")
    if not isinstance(recipient_model, str) or not recipient_model.strip():
        return None
    refusal = local_only_model_refusal(
        recipient_model,
        thread_local_only=True,
        project_id=recipient.get("project_id") or None,
        db=db,
    )
    if refusal is None or no_sensitive_content is True:
        return None
    return (
        "Before sending to a cloud-model agent, remove sensitive content and call "
        "again with no_sensitive_content: true."
    )
