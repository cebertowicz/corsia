"""A failed load settles its people once, and the live read refuses through the same error."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _cause(message_id: str):
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute("SELECT json_extract(metadata, '$.model_load_cause') FROM messages WHERE id=?", (message_id,)).fetchone()[0]
    finally:
        db.close()


async def _settled(predicate, timeout: float = 5) -> None:
    async def poll():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.asyncio
async def test_shortened_prefix_load_failure_fails_person_and_reholds_background(isolated_db, monkeypatch):
    from infra import thread_execution, unavailable_model_recovery as recovery
    from infra.database import get_db
    from infra.input_render import PreparedBatch
    from infra.preflight_attempt import prepare_idle_prefix
    from infra.thread_driver import admit_input
    from llm.local_model_load import ModelLoadFailed, Readiness
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded, message_row

    batch, _, _, provider = _claimed(monkeypatch, metadata={"sender_kind": "agent", "from_thread": "parent"})
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
        return PreparedBatch(thread_row, target, "native", False, "hi", None, [], None, SimpleNamespace(fit_ledger=ledger), None, {}, candidate)

    monkeypatch.setattr("infra.input_render.prepare_claimed_batch", render)
    monkeypatch.setattr(thread_execution, "_prepare_batch", prepare_idle_prefix)
    load = AsyncMock(side_effect=ModelLoadFailed("no_room"))
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr("ws.turn.run_post_turn_tasks", AsyncMock())
    before = asyncio.all_tasks()
    await thread_execution.drive_claimed_batch(claim)
    turns = asyncio.all_tasks() - before
    outcomes = await asyncio.wait_for(asyncio.gather(*turns, return_exceptions=True), 5)
    # The Turn ends on its own load error; settling it raises nothing else.
    assert [type(error) for error in outcomes if isinstance(error, Exception)] in ([], [ModelLoadFailed])
    assert message_row("person")[:3] == ("failed", "proved_no_write", "model_load_failed")
    assert _cause("person") == "no_room"
    # The next claim has no person in it, so the background row is held again without a load.
    await _settled(lambda: message_row("m")[:3] == ("queued", "proved_no_write", "selected_model_unavailable"))
    load.assert_awaited_once()
    provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("live, cause, expected", [
    ("no_room", "no_room", "model_load_failed"),
    ("no_memory", "no_memory", "model_load_failed"),
    (None, "unreachable", "model_load_unreachable"),
])
async def test_live_refusal_fails_person_like_the_claim(isolated_db, monkeypatch, live, cause, expected):
    from infra.thread_execution import execute_claimed_batch
    from llm.local_model_load import Readiness
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded, message_row

    batch, thread, target, provider = _claimed(monkeypatch)
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    load = AsyncMock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr("ws.turn.run_post_turn_tasks", AsyncMock())
    if live is None:
        monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=None))
    else:
        monkeypatch.setattr("llm.local_model_load.fit_readiness", lambda *_a, **_k: Readiness(live, 1))
    result = (await asyncio.gather(execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"}), return_exceptions=True))[0]
    if getattr(result, "turn_task", None) is not None:
        await asyncio.gather(result.turn_task, return_exceptions=True)
    assert message_row("m")[:3] == ("failed", "proved_no_write", expected)
    assert _cause("m") == cause
    load.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("live", ["no_room", "no_memory", None])
async def test_live_refusal_of_background_turn_takes_the_unavailable_path(isolated_db, monkeypatch, live):
    from llm.local_model_load import Readiness
    from llm.resolution import ModelUnavailableError
    from tests.test_unloaded_send_owned_turn import _claimed
    from tests.unavailable_model_support import mark_model_loaded
    from ws import turn

    _, _, target, _ = _claimed(monkeypatch)
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    if live is None:
        monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", AsyncMock(return_value=None))
    else:
        monkeypatch.setattr("llm.local_model_load.fit_readiness", lambda *_a, **_k: Readiness(live, 1))
    bind = AsyncMock()
    load = AsyncMock()
    monkeypatch.setattr(turn, "persist_turn_start_bound", bind)
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    with pytest.raises(ModelUnavailableError):
        await turn._setup_turn("t", "work", [], target=target, turn_id="background", ingress="ws", request=SimpleNamespace(load_for=()))
    bind.assert_not_awaited()
    load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unrun", [False, True])
async def test_load_settlement_error_still_settles_terminal(monkeypatch, unrun):
    from infra import send_post_admission, thread_execution, unavailable_model_recovery
    from core import turn_core

    batch = SimpleNamespace(thread_id="t", turn_id="turn", target_model="model")
    error = ValueError("load settlement failed")
    terminal = AsyncMock()
    monkeypatch.setattr(turn_core, "settle_turn_terminal", terminal)
    if unrun:
        monkeypatch.setattr("infra.input_sequencing.settle_load_failure", AsyncMock(side_effect=error))
        admission = SimpleNamespace(batch=batch, accepted=SimpleNamespace(message_id="m"))
        call = send_post_admission.fail_unrun_claim(admission, RuntimeError("initial"))
    else:
        monkeypatch.setattr(thread_execution, "run_in_db", AsyncMock(side_effect=RuntimeError("initial")))
        monkeypatch.setattr(thread_execution, "settle_load_failure", AsyncMock(side_effect=error))
        monkeypatch.setattr(unavailable_model_recovery, "resolve_or_hold", AsyncMock())
        call = thread_execution.drive_claimed_batch(batch)
    with pytest.raises(ValueError, match="load settlement failed"):
        await call
    terminal.assert_awaited_once()
