"""A person's queued send authorizes the shortened FIFO claim's model load."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_oversize_claim_loads_once_and_drains_released_person(isolated_db, monkeypatch):
    from infra import thread_execution, unavailable_model_recovery as recovery
    from infra.database import get_db
    from infra.input_render import PreparedBatch
    from infra.preflight_attempt import prepare_idle_prefix
    from infra.thread_driver import admit_input
    from llm.local_model_load import Readiness
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded, message_row

    batch, _, _, _ = _claimed(monkeypatch, metadata={"sender_kind": "agent", "from_thread": "parent"})
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    monkeypatch.setattr(recovery, "schedule_unavailable_model_recovery", lambda: None)
    await thread_execution.drive_claimed_batch(batch)
    assert message_row("m")[:3] == ("queued", "proved_no_write", "selected_model_unavailable")

    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))
    db = get_db()
    try:
        claim = admit_input("t", role="user", content="person", intent="apply_now", message_id="person", turn_id="next", boot_epoch="boot", admitted_at="t2", materialize_passive_digest=False, db=db).batch
    finally:
        db.close()
    assert [item.message_id for item in claim.items][-2:] == ["m", "person"]

    async def render(candidate, *, thread_row, target):
        ledger = SimpleNamespace(rendered_input_tokens=sum(item.message_id in {"m", "person"} for item in candidate.items), effective_window_tokens=1)
        context = SimpleNamespace(fit_ledger=ledger)
        return PreparedBatch(thread_row, target, "native", False, "hi", None, [], None, context, None, {}, candidate)

    monkeypatch.setattr("infra.input_render.prepare_claimed_batch", render)
    monkeypatch.setattr(thread_execution, "_prepare_batch", prepare_idle_prefix)
    loaded = AsyncMock(side_effect=lambda _target: mark_model_loaded(monkeypatch, "omlx@srv/m"))
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", loaded)
    monkeypatch.setattr("ws.turn.validate_native_target", AsyncMock())
    delivered = []
    done = asyncio.Event()

    async def run(request, prepared):
        from core.turn_core import settle_turn_terminal

        delivered.append(tuple(request.input_ids))
        await request.settle_delivery()
        prepared.unbind(prepared.identity)
        if len(delivered) == 1:
            assert message_row("person")[0] == "queued"
            await settle_turn_terminal(prepared.identity.turn_id, "completed")
        else:
            await settle_turn_terminal(prepared.identity.turn_id, "completed")
            done.set()
        return False

    monkeypatch.setattr("ws.turn._run_prepared_turn", run)
    monkeypatch.setattr("ws.turn.run_post_turn_tasks", AsyncMock())
    result = await thread_execution.drive_claimed_batch(claim)
    assert result.receipt.message_id == "person"
    await asyncio.wait_for(done.wait(), 5)
    assert delivered[0][-1] == "m" and "person" not in delivered[0]
    assert delivered[1] == ("person",)
    assert message_row("m")[0] == message_row("person")[0] == "delivered"
    loaded.assert_awaited_once()
