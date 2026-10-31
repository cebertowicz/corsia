"""Durable model-row versions, transcript CAS tokens, and audit projection."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from infra.database import write_transaction
from infra.queries import insert_message
from thread.model_state import (
    ModelSwitchConflict,
    ModelSwitchOutcome,
)


def _display_model(db: Any, model_id: str | None) -> str:
    """A model's human name, read on the CALLER'S connection.

    The catalog helper opens its own connection, and this runs inside the model
    update's write transaction — reading through a second connection there
    rolled the whole switch back, so the model never changed while the call
    still reported success.
    """
    identifier = str(model_id or "").strip()
    if not identifier:
        return "the selected model"
    from llm.harness_catalog import project_stored_rows

    try:
        rows = project_stored_rows(db.execute(
            "SELECT * FROM models WHERE id=? OR litellm_model=? LIMIT 1",
            (identifier, identifier),
        ).fetchall(), db=db)
    except Exception:
        return identifier
    if not rows:
        return identifier
    name = str(rows[0].get("alias") or rows[0]["display_name"] or "").strip()
    return name or identifier


def continuity_note(
    *,
    projection_mode: str | None = None,
) -> str:
    """Describe the projection proved for a model switch."""
    if projection_mode == "exact":
        return (
            "full continuity: the destination starts from CORSIA's exact "
            "persisted history."
        )
    if projection_mode == "checkpoint_tail":
        return (
            "checkpoint continuity: the destination starts from one installed "
            "checkpoint plus exact tail history."
        )
    if projection_mode == "provider_resume":
        return (
            "the same harness conversation continues with the selected model; "
            "CORSIA does not rebuild its history."
        )
    return (
        "unchanged: the selected model did not change, so no context "
        "reconstruction was required."
    )


def _insert_model_switch_breadcrumb(
    db: Any,
    thread_id: str,
    new_model: str,
    old_model: str | None,
    *,
    now: str,
    reason: str | None,
    actor_thread_id: str | None,
    actor_title: str | None,
    projection_mode: str | None = None,
) -> dict | None:
    """Insert the optional audited breadcrumb inside the model-update txn."""
    if not reason:
        return None
    note = continuity_note(
        projection_mode=projection_mode,
    )
    text = f"Switched to {_display_model(db, new_model)}"
    meta: dict = {
        "type": "activity_receipt",
        "event_type": "model.switched",
        "receipt_type": "model_switch",
        "old_model": old_model,
        "new_model": new_model,
        "reason": reason,
        "continuity": note,
    }
    if projection_mode:
        meta["continuity_mode"] = projection_mode
    if actor_thread_id:
        meta["actor_thread"] = actor_thread_id
    if actor_title:
        meta["actor_title"] = actor_title
    msg_id = str(uuid.uuid4())
    insert_message(
        thread_id,
        "system",
        text,
        meta,
        msg_id=msg_id,
        db=db,
        created_at=now,
    )
    return {"id": msg_id, "text": text, "metadata": meta, "created_at": now}


def _strictly_newer_model_version(requested: str, current: str | None) -> str:
    """Return an ISO timestamp strictly newer than the current session row."""

    requested_dt = datetime.fromisoformat(requested.replace("Z", "+00:00"))
    if requested_dt.tzinfo is not None:
        requested_dt = requested_dt.astimezone(UTC).replace(tzinfo=None)
    try:
        current_dt = datetime.fromisoformat((current or "").replace("Z", "+00:00"))
    except ValueError:
        return requested_dt.isoformat(timespec="microseconds")
    if current_dt.tzinfo is not None:
        current_dt = current_dt.astimezone(UTC).replace(tzinfo=None)
    return max(requested_dt, current_dt + timedelta(microseconds=1)).isoformat(
        timespec="microseconds",
    )


def _read_noop_model_switch_version_in_transaction(
    db,
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    now: str,
) -> str:
    """Return one durable no-op version inside the caller's write transaction."""
    if not db.in_transaction:
        raise ModelSwitchConflict("model switch no-op requires an active transaction")
    row = db.execute(
        "SELECT user_id,model,created_at,updated_at FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise ModelSwitchConflict("model switch thread vanished before no-op")
    if user_id is not None and row["user_id"] != user_id:
        raise ModelSwitchConflict("model switch thread ownership changed before no-op")
    if str(row["model"] or "").strip() != destination_model:
        raise ModelSwitchConflict("model switch source model changed before no-op")
    version = str(row["updated_at"] or row["created_at"] or "").strip()
    if not version:
        db.execute(
            "UPDATE threads SET updated_at = ? WHERE id = ? AND model = ?",
            (now, thread_id, destination_model),
        )
        version = now
    return version


def _read_noop_model_switch_version(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    now: str,
) -> str:
    """Return one durable version only while the no-op target remains current."""
    with write_transaction(None, scope="model_switch_noop_version") as db:
        version = _read_noop_model_switch_version_in_transaction(
            db,
            thread_id,
            destination_model,
            user_id=user_id,
            now=now,
        )
        return version


def _model_switch_noop_outcome(
    destination_model: str,
    *,
    committed_at: str,
) -> ModelSwitchOutcome:
    """Project the shared durable no-op result for every switch entrypoint."""
    return ModelSwitchOutcome(
        changed=False,
        old_model=destination_model,
        new_model=destination_model,
        preparation=None,
        breadcrumb=None,
        committed_at=committed_at,
    )


def _model_switch_boundary_is_clear(db, thread_id, expected_version) -> bool:
    """Fence history and Turn ownership; selected FIFO rows have their own proof."""
    from thread.transcript_order import destructive_boundary_is_clear, destructive_boundary_version

    current = destructive_boundary_version(db, thread_id)
    return current[:2] == expected_version[:2] and destructive_boundary_is_clear(
        db, thread_id, current,
    )


def _model_switch_transcript_version(
    db,
    thread_id: str,
) -> tuple[int, int, int, int]:
    """Return a compact append/delete/retention token for final CAS."""
    from core.thread_lifecycle import TOOL_TRIM_MARKER

    row = db.execute(
        """
        SELECT s.next_message_seq,
               COUNT(m.id) AS message_count,
               COALESCE(MAX(m.seq), 0) AS max_seq,
               COALESCE(SUM(
                   CASE WHEN m.role = 'tool'
                         AND substr(m.content, 1, ?) = ?
                        THEN 1 ELSE 0 END
               ), 0) AS retention_stub_count
        FROM threads AS s
        LEFT JOIN messages AS m ON m.thread_id = s.id AND m.seq IS NOT NULL
        WHERE s.id = ?
        GROUP BY s.id
        """,
        (len(TOOL_TRIM_MARKER), TOOL_TRIM_MARKER, thread_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"thread does not exist: {thread_id}")
    return (
        int(row["next_message_seq"]),
        int(row["message_count"]),
        int(row["max_seq"]),
        int(row["retention_stub_count"]),
    )


def _read_model_switch_transcript_version_for_thread(
    thread_id: str,
) -> tuple[int, int, int, int]:
    """Read a current transcript version through an isolated database handle."""
    from infra.database import get_db

    db = get_db()
    try:
        return _model_switch_transcript_version(db, thread_id)
    finally:
        db.close()


def select_first_model(
    thread_id: str,
    destination_model: str,
    *,
    user_id: str | None,
    now: str,
    account_id: str | None = None,
    actor_thread_id: str | None = None,
    first_run_wake: str | None = None,
) -> bool:
    """Give a thread with no model its first one; ``False`` when it has one.

    There is no source to migrate, checkpoint or drain, so the selection
    commits directly. Inputs accepted while no model was selected targeted
    nothing; they take the new model in the same transaction. ``first_run_wake``
    replaces a held first-run wake's content in that same write, before any
    Turn can read it.
    """
    from infra.model_switch_inputs import adopt_first_model
    from llm.resolution import resolve_execution_target
    from thread.model_state import ModelSwitchPreparationError

    with write_transaction(None, scope="model_first_selection") as db:
        row = db.execute(
            "SELECT user_id,model,switch_operation_id FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise ModelSwitchConflict(f"model selection thread does not exist: {thread_id}")
        if user_id is not None and row["user_id"] != user_id:
            raise ModelSwitchConflict(f"model selection thread is not owned by {user_id}")
        if str(row["model"] or "").strip() or row["switch_operation_id"] is not None:
            return False
        try:
            target = resolve_execution_target(destination_model, db=db)
        except Exception as exc:
            raise ModelSwitchPreparationError(
                f"model selection destination resolution failed: {exc}"
            ) from exc
        if not target.available:
            reason = target.availability_reason or "destination unavailable"
            raise ModelSwitchPreparationError(
                f"model selection destination is unavailable: {reason}"
            )
        from connections.account_selection import bind_model_account

        if first_run_wake is not None:
            from onboarding.first_run_opening import seed_first_model_opening

            # This is the only write that can release the held wake. Persist the
            # greeting before model adoption makes the wake runnable.
            seed_first_model_opening(thread_id, db=db)
        bind_model_account(thread_id, destination_model, account_id=account_id,
                           actor_thread_id=actor_thread_id, db=db)
        if first_run_wake is not None:
            from infra.onboarding_events import fill_held_first_run_wake

            fill_held_first_run_wake(thread_id, first_run_wake, db=db)
        adopt_first_model(db, thread_id, destination_model, now=now)
        return True
