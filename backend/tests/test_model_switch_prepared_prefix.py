"""The destination proves a fitting FIFO prefix, not every later queued input."""

import pytest

from shared_send_fixtures import _insert_native_model, _insert_session, _install_fake_native_context_agent


@pytest.mark.asyncio
async def test_real_destination_render_selects_only_the_fitting_prefix(isolated_db, monkeypatch):
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.model_switches import request_switch
    from thread.model_prepare import prepare_model_switch

    _install_fake_native_context_agent(monkeypatch)
    _insert_native_model("native/source")
    db = get_db()
    try:
        db.execute("UPDATE models SET provider_model_id='source' WHERE id='native/source'")
        db.commit()
    finally:
        db.close()


    _insert_native_model("native/target")
    _insert_session("prefix-switch", model="native/source", spawned_by=None)
    db = get_db()
    try:
        db.execute("UPDATE models SET context_window=20000 WHERE id='native/target'")
        db.commit()
        request_switch(
            "prefix-switch", "native/target", operation_id="prefix-operation",
            requested_at="t1", db=db,
        )
        for message_id, content in (("first", "Please continue."), ("later", "large " * 50000)):
            accept_thread_input(
                "prefix-switch", role="user", content=content, intent="apply_now",
                message_id=message_id, db=db,
            )
        before = [tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY input_order")]
    finally:
        db.close()
    preparation = await prepare_model_switch("prefix-switch", "native/target", user_id="u-test")
    assert preparation is not None
    assert [member[0] for member in preparation.input_proof.members] == ["first"]
    assert preparation.destination_fit_ledger.rendered_input_tokens <= 20000
    db = get_db()
    try:
        assert [tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY input_order")] == before
        assert db.execute("SELECT model FROM threads WHERE id='prefix-switch'").fetchone()[0] == "native/source"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_duplicate_wake_cannot_fail_the_newer_preparation(isolated_db, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.model_switches import request_switch, read_switch
    from thread import model_pending

    _install_fake_native_context_agent(monkeypatch)
    _insert_native_model("native/source")
    db = get_db()
    db.execute("UPDATE models SET provider_model_id='source' WHERE id='native/source'")
    db.commit()
    db.close()
    _insert_native_model("native/target")
    _insert_session("duplicate-switch", model="native/source", spawned_by=None)
    request_switch("duplicate-switch", "native/target", operation_id="duplicate-operation", requested_at="t1")
    accept_thread_input("duplicate-switch", role="user", content="Continue", intent="apply_now", message_id="pending")
    real_prepare = model_pending.prepare_model_switch
    first_prepared, second_prepared, release_second = (asyncio.Event() for _ in range(3))
    calls = 0

    async def prepare(*args, **kwargs):
        nonlocal calls
        calls += 1
        ordinal = calls
        result = await real_prepare(*args, **kwargs)
        if ordinal == 1:
            first_prepared.set()
            await second_prepared.wait()
        else:
            second_prepared.set()
            await release_second.wait()
        return result

    monkeypatch.setattr(model_pending, "prepare_model_switch", prepare)
    monkeypatch.setattr(model_pending, "_drive_destination", AsyncMock())
    first = asyncio.create_task(model_pending.drive_pending_model_switch("duplicate-switch"))
    second = None
    try:
        await asyncio.wait_for(first_prepared.wait(), 2)
        second = asyncio.create_task(model_pending.drive_pending_model_switch("duplicate-switch"))
        await asyncio.wait_for(first, 2)
        assert read_switch("duplicate-switch") is not None, "stale generation failed the live preparation"
        release_second.set()
        await asyncio.wait_for(second, 2)
        assert read_switch("duplicate-switch") is None
        db = get_db()
        assert db.execute("SELECT model FROM threads WHERE id='duplicate-switch'").fetchone()[0] == "native/target"
        db.close()
        assert calls == 2
    finally:
        release_second.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
