"""SQL-only boundaries for the existing durable publication intent phases."""

from datetime import datetime, timezone
from typing import Any

from infra.database import write_transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(db, intent: dict[str, Any]) -> None:
    """Commit the recovery intent before applying any filesystem effects."""
    with write_transaction(db, scope="stage_durable_write"):
        db.execute(
            "INSERT INTO durable_write_intents "
            "(id,operation,phase,actor_thread_id,artifact_type,external_id,"
            "target_path,stage_path,backup_path,source_path,description,"
            "content_sha256,created_parent,created_at,updated_at) "
            "VALUES (:id,:operation,'staged',:actor_thread_id,:artifact_type,:external_id,"
            ":target_path,:stage_path,:backup_path,:source_path,:description,"
            ":content_sha256,:created_parent,:now,:now)",
            {**intent, "now": _now()},
        )


def record_source_hash(db, intent_id: str, content_hash: str) -> None:
    """Record the claimed source bytes before publishing them."""
    with write_transaction(db, scope="hash_durable_write"):
        db.execute(
            "UPDATE durable_write_intents SET content_sha256=?,updated_at=? WHERE id=?",
            (content_hash, _now(), intent_id),
        )


def mark_applied(db, intent_id: str) -> None:
    """Record filesystem application before registry registration."""
    with write_transaction(db, scope="apply_durable_write"):
        db.execute(
            "UPDATE durable_write_intents SET phase='file_applied',updated_at=? WHERE id=?",
            (_now(), intent_id),
        )


def mark_undoing(db, intent_id: str, phase: int) -> None:
    """Persist the existing before-restore and after-restore undo cuts."""
    with write_transaction(db, scope="undo_durable_write"):
        db.execute(
            "UPDATE durable_write_intents SET undoing=?,updated_at=? WHERE id=?",
            (phase, _now(), intent_id),
        )


def delete(db, intent_id: str) -> None:
    """Remove an intent only after its filesystem cleanup succeeds."""
    with write_transaction(db, scope="complete_durable_write"):
        db.execute("DELETE FROM durable_write_intents WHERE id=?", (intent_id,))
