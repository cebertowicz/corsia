"""Seed helpers for the unavailable-selected-model hold and recovery tests."""

from __future__ import annotations

from infra.thread_execution import drive_claimed_batch as _drive_claimed_batch


def seed_model(
    model_id: str,
    *,
    backend: str = "network",
    provider: str = "omlx",
    available: int = 1,
    enabled: int = 1,
) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,"
            "litellm_model,display_name,backend,source,enabled,available,"
            "context_window,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?, 'test',?,?,32768,'t0','t0')",
            (
                model_id, "native", provider, model_id.rpartition("/")[2],
                model_id, model_id, backend, enabled, available,
            ),
        )
        db.commit()
    finally:
        db.close()


def set_model_available(model_id: str, available: int) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("UPDATE models SET available=? WHERE id=?", (available, model_id))
        db.commit()
    finally:
        db.close()


def mark_model_loaded(monkeypatch, model_id: str, *, loaded: bool = True) -> None:
    """Publish the online server and the cached loaded fact (or its absence) separately from disk availability."""
    from infra.database import get_db
    from llm import network_discovery

    server_id, _, provider_model = model_id.partition("@")[2].partition("/")
    db = get_db()
    db.execute(
        "INSERT INTO network_servers (id,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at) "
        "VALUES (?,?,?,8210,'online','t0','t0','t0') ON CONFLICT(id) DO UPDATE SET status='online'",
        (server_id, server_id, server_id),
    )
    db.commit()
    db.close()
    monkeypatch.setitem(network_discovery._LOADED_BY_SERVER, server_id, {provider_model} if loaded else set())


def admit_held_candidate(thread_id: str, model: str, message_id: str):
    """Create a thread on ``model`` and claim its first input for a Turn."""
    from infra.database import get_db
    from infra.thread_driver import admit_input

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,'t0','t0')",
            (thread_id, "user-1", model),
        )
        db.commit()
        admission = admit_input(
            thread_id, role="user", content="hello", intent="apply_now",
            message_id=message_id, turn_id=f"{message_id}-turn",
            boot_epoch="boot-1", admitted_at="t1", db=db,
        )
    finally:
        db.close()
    assert admission.batch is not None
    return admission.batch


def message_row(message_id: str) -> tuple:
    from infra.database import get_db

    db = get_db()
    try:
        return tuple(db.execute(
            "SELECT delivery_state,write_outcome,delivery_error,active_turn_id "
            "FROM messages WHERE id=?",
            (message_id,),
        ).fetchone())
    finally:
        db.close()


async def hold_head(monkeypatch, thread_id: str, model: str, message_id: str, **seed):
    """Seed an unavailable ``model`` and hold ``message_id`` as its FIFO head."""
    from unittest.mock import Mock

    import infra.unavailable_model_recovery as recovery

    monkeypatch.setattr(recovery, "schedule_unavailable_model_recovery", Mock())
    seed_model(model, available=0, **seed)
    if model.startswith("omlx@"):
        mark_model_loaded(monkeypatch, model)
    held = await _drive_claimed_batch(
        admit_held_candidate(thread_id, model, message_id),
    )
    assert held.receipt.message_id == message_id
    assert message_row(message_id) == HELD
    return held


HELD = ("queued", "proved_no_write", "selected_model_unavailable", None)
