"""Journal a content edit for the reviewers still waiting on the document (DCOL-FR10.2).

Every route that stores a document's bytes passes the digest before and after
the write, so a re-save of identical bytes is not an edit. Only a document with
an unsettled review request is journaled, so owners of other documents see no
new rows.
"""

from __future__ import annotations

import sqlite3

from doceditor.document_scope import UI_THREAD
from doceditor.note_names import actor_name, document_name
from registry import models


def journal_reviewed_edit(
    db: sqlite3.Connection,
    artifact: dict | sqlite3.Row,
    *,
    previous_sha256: str | None,
    new_sha256: str | None,
    actor_thread_id: str | None,
) -> bool:
    """Append ``content_edited`` when the bytes changed while a review of the document is open.

    "Open" is Plans' answer (``review_is_open``), the same one the reviewer watch
    reads, so no row is written that no reader would ever admit.
    """
    if not new_sha256 or new_sha256 == previous_sha256:
        return False
    from plans import public as plans_public

    artifact_id = str(artifact["id"])
    unsettled = db.execute(
        # The join drops a request whose review node no longer exists, as the reviewer watch does.
        "SELECT request.node_id FROM doc_review_requests AS request "
        "JOIN plan_nodes AS node ON node.id=request.node_id WHERE request.artifact_id=? "
        "AND request.cancelled_at IS NULL AND request.answered_batch_id IS NULL",
        (artifact_id,),
    ).fetchall()
    if not any(plans_public.review_is_open(db, str(row["node_id"])) for row in unsettled):
        return False
    actor = None if actor_thread_id in (None, UI_THREAD) else actor_thread_id
    models.record_artifact_change(
        artifact_id=artifact_id,
        internal_event="content_edited",
        external_id=None,
        summary=f"{actor_name(db, actor)} edited {document_name(artifact)}.",
        provenance={"source": "document_edit"},
        actor_thread_id=actor,
        db=db,
    )
    return True
