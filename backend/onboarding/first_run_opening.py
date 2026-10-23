"""The app's scripted greeting: preview for warming, persist at the first pick."""

from __future__ import annotations

import sqlite3

OPENING_REFERENCE = "skills/conductor-bootstrap/references/first-run-opening.md"
OPENING_PREFIX = "Hi {name}."


def opening_message_id(thread_id: str) -> str:
    """Return the stable id for the first app greeting in ``thread_id``."""
    return f"first-run-opening:{thread_id}"


def opening_message(name: str | None = None) -> str:
    """Return the packaged opening, with its optional person-facing prefix."""
    from infra.system_package import system_resource_source

    text = system_resource_source(OPENING_REFERENCE).read_text(encoding="utf-8").strip()
    return f"{OPENING_PREFIX.format(name=name)}\n\n{text}" if name else text


def pending_warm_opening(conductor_id: str, *, db: sqlite3.Connection) -> str | None:
    """Return the absent app greeting only while its pristine first-run wake is held."""
    from infra import config, user_profile
    from infra.onboarding_events import first_run_message_id, held_first_run_path

    if held_first_run_path(conductor_id, db=db) != "app":
        return None
    opening_id = opening_message_id(conductor_id)
    if db.execute(
        "SELECT 1 FROM messages WHERE id=? AND thread_id=?", (opening_id, conductor_id),
    ).fetchone() is not None:
        return None
    # Setup plan changes queue a passive digest before the first-run wake.
    # Only that pending notification is compatible with an untouched conversation.
    if db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? "
        "AND NOT COALESCE(id=? OR (id=? AND delivery_state='queued') OR ("
        "role='system' AND delivery_state='queued' "
        "AND json_extract(metadata,'$.type')='context_event' "
        "AND json_extract(metadata,'$.event_type')='context.digest' "
        "AND json_extract(metadata,'$.event_disposition')='passive'), 0) LIMIT 1",
        (conductor_id, opening_id, first_run_message_id(conductor_id)),
    ).fetchone() is not None:
        return None
    return opening_message(user_profile.profile_name(config.WORKSPACE) or None)


def seed_first_model_opening(conductor_id: str, *, db: sqlite3.Connection) -> None:
    """Add the greeting on the model transaction's connection before wake release."""
    from infra.queries import insert_message
    from infra.turn_provenance import SERVER_SCRIPTED_REASON

    opening = pending_warm_opening(conductor_id, db=db)
    if opening is not None:
        insert_message(
            conductor_id, "assistant", opening,
            {"for_user": True, "first_run_opening": True, "turn_unbound_reason": SERVER_SCRIPTED_REASON},
            msg_id=opening_message_id(conductor_id), db=db,
        )
