"""A model refusal after a mixed held FIFO prefix has been claimed."""
import pytest


@pytest.mark.asyncio
async def test_mixed_refusal_keeps_background_held(isolated_db, monkeypatch):
    from infra import thread_execution, unavailable_model_recovery as recovery
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from llm.local_model_load import Readiness, ModelLoadFailed
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded, message_row

    batch, _, _, _ = _claimed(monkeypatch, metadata={"sender_kind": "agent", "from_thread": "parent"})
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    monkeypatch.setattr(recovery, "schedule_unavailable_model_recovery", lambda: None)
    await thread_execution.drive_claimed_batch(batch)
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))
    db = get_db()
    try:
        claim = admit_input("t", role="user", content="person", intent="apply_now", message_id="person", turn_id="next", boot_epoch="boot", admitted_at="t2", materialize_passive_digest=False, db=db).batch
    finally:
        db.close()
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("no_room", 1))
    with pytest.raises(ModelLoadFailed):
        await thread_execution.drive_claimed_batch(claim)
    assert message_row("m")[:3] == ("queued", "proved_no_write", "selected_model_unavailable")


@pytest.mark.asyncio
async def test_post_ack_mixed_load_failure(isolated_db, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from infra import thread_execution, unavailable_model_recovery as recovery
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from llm.local_model_load import ModelLoadFailed, Readiness
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded, message_row

    batch, thread, target, provider = _claimed(
        monkeypatch, metadata={"sender_kind": "agent", "from_thread": "parent"},
    )
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    monkeypatch.setattr(recovery, "schedule_unavailable_model_recovery", lambda: None)
    await thread_execution.drive_claimed_batch(batch)
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))
    db = get_db()
    try:
        claim = admit_input(
            "t", role="user", content="person", intent="apply_now", message_id="person",
            turn_id="next", boot_epoch="boot", admitted_at="t2",
            materialize_passive_digest=False, db=db,
        ).batch
    finally:
        db.close()
    load = AsyncMock(side_effect=ModelLoadFailed("unreachable"))
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    result = await thread_execution.execute_claimed_batch(
        claim, thread_row=thread, target=target, user={"id": "u"},
    )
    await asyncio.gather(result.turn_task, return_exceptions=True)
    assert message_row("person")[:3] == ("failed", "proved_no_write", "model_load_unreachable")
    assert message_row("m")[:3] == ("queued", "proved_no_write", "selected_model_unavailable")
    db = get_db()
    try:
        cause = db.execute(
            "SELECT json_extract(metadata, '$.model_load_cause') FROM messages WHERE id='person'"
        ).fetchone()[0]
        assert cause == "unreachable"
    finally:
        db.close()
    load.assert_awaited_once()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_fenced_release_accepts_background_subset(isolated_db, monkeypatch):
    from dataclasses import replace
    from infra import thread_execution, unavailable_model_recovery as recovery
    from infra.database import get_db
    from infra.input_writes import fence_batch_write, release_batch_after_proved_no_write
    from infra.thread_driver import admit_input
    from llm.local_model_load import Readiness
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded

    batch, _, _, _ = _claimed(monkeypatch, metadata={"sender_kind": "agent", "from_thread": "parent"})
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    monkeypatch.setattr(recovery, "schedule_unavailable_model_recovery", lambda: None)
    await thread_execution.drive_claimed_batch(batch)
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))
    db = get_db()
    try:
        claim = admit_input("t", role="user", content="person", intent="apply_now", message_id="person", turn_id="next", boot_epoch="boot", admitted_at="t2", materialize_passive_digest=False, db=db).batch
    finally:
        db.close()
    await fence_batch_write(claim, attempt_id="attempt", started_at="t3")
    background = replace(claim, items=tuple(item for item in claim.items if item.message_id == "m"))
    release_batch_after_proved_no_write(background, attempt_id="attempt", error=None)
    db = get_db()
    try:
        rows = db.execute("SELECT id,active_turn_id,delivery_attempt_id FROM messages WHERE id IN ('m','person') ORDER BY input_order").fetchall()
        assert [tuple(row) for row in rows] == [("m", None, None), ("person", "next", "attempt")]
    finally:
        db.close()
