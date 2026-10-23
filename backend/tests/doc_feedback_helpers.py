"""Focused setup helpers for document-feedback batch tests."""

from __future__ import annotations

import uuid

from doceditor import comments, feedback_batches
from infra.database import write_transaction
from registry import models as registry_models

MODEL = "openai/test-checkpoint"


def seed_thread(db, thread_id: str, *, title: str | None = None, model: str | None = MODEL) -> str:
    """Insert one active test thread with an addressable model by default."""
    db.execute(
        """INSERT INTO threads(id,user_id,title,model,created_at,updated_at)
           VALUES (?,?,?,?,?,?)""",
        (thread_id, "owner-token", title or thread_id, model, "2026-09-29T00:00:00+00:00", "2026-09-29T00:00:00+00:00"),
    )
    return thread_id


def seed_document(
    db,
    owner: str,
    *,
    name: str | None = None,
    retention: str = "ephemeral",
) -> str:
    """Register one workspace document to ``owner`` and return its identity id."""
    label = name or f"doc-{uuid.uuid4().hex[:8]}.md"
    artifact = registry_models.register_artifact(
        integration="local",
        type="document",
        external_id=f"/workspace/shared/{label}",
        parent_thread_id=owner,
        title=label,
        path=f"/workspace/shared/{label}",
        retention=retention,
        metadata={"zone": "workspace"},
        db=db,
    )
    db.commit()
    return str(artifact["id"])


def add_comment(
    db,
    artifact_id: str,
    *,
    author_kind: str = "agent",
    author_id: str | None = "author",
    author: str = "Author",
    parent_id: str | None = None,
    replacement: str | None = None,
    submit: bool = False,
    client_id: str | None = None,
) -> tuple[dict, dict]:
    """Create and batch one comment in one transaction."""
    with write_transaction(db, scope="test_add_feedback_comment") as connection:
        row, created = comments.create_with_status(
            artifact_id,
            body="Please check this point.",
            author=author,
            author_kind=author_kind,
            author_thread_id=author_id if author_kind == "agent" else None,
            parent_id=parent_id,
            anchor_text=None if parent_id else "quoted source",
            from_off=None if parent_id else 0,
            to_off=None if parent_id else 13,
            replacement=replacement,
            client_id=client_id or str(uuid.uuid4())[:12],
            announce=author_kind != "user",
            db=connection,
        )
        feedback = feedback_batches.record_comment(
            connection, row, created=created, submit=submit,
        )
    return row, feedback


def batches(db, *, state: str | None = None) -> list[dict]:
    """Return feedback batches in stable creation order."""
    sql = "SELECT * FROM doc_feedback_batches"
    params: tuple = ()
    if state is not None:
        sql += " WHERE state=?"
        params = (state,)
    sql += " ORDER BY created_at,id"
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def linked_comment_ids(db, batch_id: str) -> list[str]:
    """Return one batch's ordered comment ids."""
    return [
        str(row["comment_id"])
        for row in db.execute(
            "SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=? ORDER BY comment_id",
            (batch_id,),
        ).fetchall()
    ]


def add_person_comment(db, artifact_id: str, *, client_id: str | None = None) -> tuple[dict, dict]:
    """Create and immediately deliver a person's comment as the post-response task does."""
    row, feedback = add_comment(
        db, artifact_id, author_kind="user", author_id=None, author="Steve", client_id=client_id,
    )
    if feedback["batch_ids"]:
        feedback["woken"].extend(feedback_batches.deliver_person_comment(db, feedback["batch_ids"]))
    return row, feedback
