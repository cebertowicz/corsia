"""Small fixtures for an agent's document-backed user hold."""

from __future__ import annotations

import uuid

from plans import service
from registry import models


def document_ask(db, owner_thread: str) -> str:
    """Register and return one document ask owned by ``owner_thread``."""
    external_id = f"test-user-ask-{uuid.uuid4().hex}.md"
    models.register_artifact(
        integration="workspace",
        type="document",
        external_id=external_id,
        parent_thread_id=owner_thread,
        title="Review the test document",
        db=db,
    )
    row = db.execute(
        "SELECT id FROM artifact_identities WHERE integration='workspace' "
        "AND type='document' AND external_id=?",
        (external_id,),
    ).fetchone()
    return str(row["id"])


def with_actionable_user_ask(db, plan_id: str, operation: str, params: dict, actor) -> dict:
    """Give an agent's user-block fixture the document its request requires."""
    if (
        operation != "block_node"
        or params.get("owner_kind") != "user"
        or actor.kind != "thread"
        or params.get("ask_artifact_id")
    ):
        return params
    plan = service._plan_row(db, plan_id)
    return {
        **{key: value for key, value in params.items() if key != "owner_id"},
        "ask_artifact_id": document_ask(db, str(plan["owner_id"])),
    }
