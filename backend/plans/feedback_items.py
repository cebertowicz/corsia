"""Read one document-feedback batch: its items in order, and the labels nodes and notes show."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from registry.identity_metadata import last_written_sha256, sha256_file
from registry.shared_edit_classification import combine_capped_diffs, capped_unified_diff


def batch_value(batch: Any, key: str) -> Any:
    return batch[key] if hasattr(batch, "keys") else getattr(batch, key)


def artifact_row(db, artifact_id: str):
    return db.execute(
        "SELECT title,path,external_id,metadata FROM artifact_identities WHERE id=?",
        (artifact_id,),
    ).fetchone()


def artifact_name(db, artifact_id: str) -> str:
    from doceditor.note_names import document_name

    return document_name(artifact_row(db, artifact_id), fallback="document")


def current_artifact_hash(db, artifact_id: str) -> str | None:
    row = artifact_row(db, artifact_id)
    if row is None:
        return None
    raw_path = row["path"] or row["external_id"]
    if raw_path:
        try:
            return sha256_file(Path(str(raw_path)))
        except OSError:
            pass
    return last_written_sha256(row["metadata"])


def load_feedback_items(db, batch: Any) -> list[dict[str, Any]]:
    """Load the stable chronological union of comments and a renderable edit."""
    batch_id = str(batch_value(batch, "id"))
    artifact_id = str(batch_value(batch, "artifact_id"))
    comments = [
        {
            **dict(row),
            "kind": "comment",
            "sort_at": row["event_at"] if row["event"] == "reopened" else row["created_at"],
            "sort_id": row["id"],
        }
        for row in db.execute(
            """SELECT c.*,bi.event,bi.event_at,bi.event_by
               FROM doc_feedback_batch_items bi
               JOIN doc_comments c ON c.id=bi.comment_id
               WHERE bi.batch_id=? ORDER BY c.created_at,c.id""",
            (batch_id,),
        ).fetchall()
    ]
    edit_row = db.execute(
        "SELECT * FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,)
    ).fetchone()
    items: list[dict[str, Any]] = comments
    if edit_row is not None:
        edit = dict(edit_row)
        net_empty = (
            not edit["prior_segments_diff"]
            and edit["latest_sha256"] == edit["baseline_sha256"]
        )
        if not net_empty:
            artifact = artifact_row(db, artifact_id)
            raw_path = (
                artifact["path"] or artifact["external_id"]
                if artifact is not None else "document"
            )
            current_segment = capped_unified_diff(
                Path(str(raw_path)),
                str(edit["baseline_text"]).encode("utf-8"),
                str(edit["latest_text"]).encode("utf-8"),
            )
            edit.update(
                kind="edit",
                sort_at=edit["first_at"],
                sort_id=edit["batch_id"],
                rendered_diff=combine_capped_diffs(
                    str(edit["prior_segments_diff"]), current_segment,
                ),
                diverged=(
                    (current := current_artifact_hash(db, artifact_id)) is not None
                    and current != edit["latest_sha256"]
                ),
            )
            items.append(edit)
    return sorted(
        items,
        key=lambda item: (str(item["sort_at"]), str(item["kind"]), str(item["sort_id"])),
    )


def feedback_display(
    db,
    batch: Any,
    items: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Resolve the shared author and document labels for nodes and notes."""
    loaded = items if items is not None else load_feedback_items(db, batch)
    if batch_value(batch, "author_kind") == "user":
        first = loaded[0] if loaded else None
        author = (
            str(first["event_by"] or "The user")
            if first is not None and first["kind"] == "comment" and first.get("event") == "reopened"
            else str(first["author"])
            if first is not None and first["kind"] == "comment"
            else str(first["actor_display"])
            if first is not None
            else "The user"
        )
    else:
        row = db.execute(
            "SELECT title FROM threads WHERE id=?", (batch_value(batch, "author_id"),)
        ).fetchone()
        author = ((row["title"] if row else None) or batch_value(batch, "author_id")).strip()
    return author, artifact_name(db, str(batch_value(batch, "artifact_id")))


def feedback_kind_counts(items: list[dict[str, Any]]) -> tuple[int, int]:
    """Return the renderable edit and comment counts for note copy."""
    return (
        sum(item["kind"] == "edit" for item in items),
        sum(item["kind"] == "comment" for item in items),
    )


def feedback_item_counts(items: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return edit, ordinary-comment, and reopened-comment counts for one batch."""
    edits, comments = feedback_kind_counts(items)
    reopened = sum(
        item["kind"] == "comment" and item.get("event") == "reopened" for item in items
    )
    return edits, comments - reopened, reopened
