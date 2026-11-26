"""The single writer for approved artifact records."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def record_approval(
    db: sqlite3.Connection, *, artifact_id: str, node_id: str | None,
    source: str, source_id: str | None, doc_sha256: str, images: list[dict], prepared=None,
) -> None:
    """Record one prepared version in the caller's transaction.

    A person's latest direct save supersedes their previous direct save. Approval
    and suggestion rows remain part of the record.
    """
    if source == "save":
        from infra.write_effects import after_commit
        from plans.approved_versions import _copies

        previous = db.execute(
            "SELECT a.*,i.path,i.external_id FROM artifact_approvals a "
            "JOIN artifact_identities i ON i.id=a.artifact_id "
            "WHERE a.artifact_id=? AND a.source='save'", (artifact_id,),
        ).fetchall()
        copies = [copy for row in previous for copy in _copies(row, db) if copy.get("sha256")]
        db.execute("DELETE FROM artifact_approvals WHERE artifact_id=? AND source='save'", (artifact_id,))
        if copies:
            after_commit(db, lambda: _delete_unreferenced(db, copies))
    db.execute(
        "INSERT INTO artifact_approvals (artifact_id,node_id,source,source_id,doc_sha256,images,at) "
        "VALUES (?,?,?,?,?,?,?)",
        (artifact_id, node_id, source, source_id, doc_sha256, json.dumps(images),
         datetime.now(timezone.utc).isoformat()),
    )
    if prepared is not None:
        from infra.write_effects import after_commit
        after_commit(db, prepared.restore_missing)


def _delete_unreferenced(db: sqlite3.Connection, copies: list[dict]) -> None:
    """Recheck committed records before removing superseded copies."""
    from pathlib import Path
    from plans.approval_copies import copy_lock

    with copy_lock:
        for copy in copies:
            digest = copy["sha256"]
            page = db.execute("SELECT 1 FROM artifact_approvals WHERE doc_sha256=? LIMIT 1", (digest,)).fetchone()
            image = db.execute(
                "SELECT 1 FROM artifact_approvals a, json_each(a.images) i "
                "WHERE json_extract(i.value, '$.sha256')=? LIMIT 1", (digest,),
            ).fetchone()
            if page is None and image is None:
                Path(copy["copy_path"]).unlink(missing_ok=True)


class ApprovalChangedDuringSave(RuntimeError):
    """The approval state changed between preparation and registration."""


def record_saved_version(db: sqlite3.Connection, artifact_id: str, prepared, accepted: tuple[str, ...] = (), *, retry: bool = False) -> bool:
    """Recheck and record a person's save in its registration transaction."""
    exists = db.execute("SELECT 1 FROM artifact_approvals WHERE artifact_id=? LIMIT 1", (artifact_id,)).fetchone() is not None
    if exists != (prepared is not None) and (exists or retry):
        raise ApprovalChangedDuringSave("Approval changed during save. Please try again.")
    if not exists or prepared is None:
        return False
    if accepted:
        from plans.user_ask import raised_asks
        if raised_asks(db, artifact_id):
            return False
        for suggestion_id in accepted:
            record_approval(db, artifact_id=artifact_id, node_id=None, source="suggestion",
                            source_id=suggestion_id, doc_sha256=prepared.doc_sha256, images=prepared.images, prepared=prepared)
    else:
        record_approval(db, artifact_id=artifact_id, node_id=None, source="save", source_id=None,
                        doc_sha256=prepared.doc_sha256, images=prepared.images, prepared=prepared)
    return True
