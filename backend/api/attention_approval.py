"""Prepare document approval copies before clearing attention."""

from infra.database import get_db
from plans import public
from registry import attention


def clear_with_approval(artifact_id: str, body, woken: list[str]):
    db = get_db()
    prepared = None
    try:
        if body.outcome == "answer" and body.verdict in {"approved", "approved_with_changes"}:
            prepared = public.prepare_bar_approval(artifact_id, body.doc_sha256, db=db)
        return attention.clear_attention(
            artifact_id, outcome=body.outcome, condition=body.condition,
            reason=body.reason, verdict=body.verdict, note=body.note,
            hold_ids=body.hold_ids, require_hold_ids=True, drives=woken,
            db=db, approved_version=prepared,
        )
    except Exception:
        if prepared is not None:
            prepared.discard()
        raise
    finally:
        db.close()
