"""Idle batching keeps one fitting FIFO prefix and truthful caller receipts."""

from dataclasses import replace

import pytest


def _claim_pair(db, *, first_intent="queue_after_turn", passive=False):
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    accept_thread_input("main-1", role="system" if passive else "user", content="first " + "word " * 100,
                        intent=first_intent, message_id="first", db=db,
                        metadata={"type": "context_event", "event_disposition": "passive"} if passive else None)
    accept_thread_input(
        "main-1", role="user", content="second " + "word " * 100,
        intent="apply_now", message_id="second", db=db,
    )
    batch = start_idle_turn("main-1", turn_id="turn-1", boot_epoch="test", started_at="t1", db=db)
    assert batch is not None
    return batch, dict(db.execute("SELECT * FROM threads WHERE id='main-1'").fetchone())


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-code/opus", "codex/gpt-5-codex"])
async def test_idle_preparation_keeps_fitting_prefix_and_same_refresh_input(main_db, model):
    from infra.input_render import prepare_claimed_batch
    from infra.thread_execution import _prepare_batch
    from llm.resolution import resolve_execution_target

    main_db.execute("UPDATE threads SET model=? WHERE id='main-1'", (model,))
    main_db.commit()
    batch, row = _claim_pair(main_db)
    target = resolve_execution_target(model, db=main_db)
    one = replace(batch, items=batch.items[:1])
    reference = await prepare_claimed_batch(one, thread_row=row, target=target)
    window = reference.prepared_context.fit_ledger.rendered_input_tokens + 10
    target = replace(target, context_window=window)
    prepared = await _prepare_batch(batch, thread_row=row, target=target)

    assert "first" in prepared.content and "second" not in prepared.content
    assert prepared.batch == one
    assert prepared.prepared_context.fit_ledger.rendered_input_tokens <= window
    refreshed = await prepared.refresh_prepared_context()
    assert refreshed.rendered.fit_current_input == prepared.prepared_context.rendered.fit_current_input
    rows = main_db.execute("SELECT id,active_turn_id,delivery_state,delivery_attempt_id,seq "
                           "FROM messages ORDER BY input_order").fetchall()
    assert [tuple(row) for row in rows] == [
        ("first", "turn-1", "queued", None, None),
        ("second", None, "queued", None, None),
    ]


@pytest.mark.asyncio
async def test_no_current_fit_keeps_smallest_active_prefix_for_checkpoint(main_db):
    from infra.thread_execution import _prepare_batch
    from llm.resolution import resolve_execution_target

    batch, row = _claim_pair(main_db)
    target = replace(resolve_execution_target(batch.target_model, db=main_db), context_window=1)
    prepared = await _prepare_batch(batch, thread_row=row, target=target)
    assert [item.message_id for item in prepared.batch.items] == ["first"]
    assert prepared.prepared_context.fit_ledger.rendered_input_tokens > 1
    assert tuple(main_db.execute("SELECT active_turn_id,write_started_at FROM messages "
                                "WHERE id='second'").fetchone()) == (None, None)


@pytest.mark.asyncio
async def test_dispatching_prefix_returns_original_tail_receipt(main_db, monkeypatch):
    from infra.input_render import prepare_claimed_batch
    from infra.input_writes import settle_batch_write
    from infra.send_types import InboxQueuedInput
    from infra import thread_execution
    from llm.resolution import resolve_execution_target

    batch, row = _claim_pair(main_db)
    target = resolve_execution_target(batch.target_model, db=main_db)
    reference = await prepare_claimed_batch(replace(batch, items=batch.items[:1]), thread_row=row, target=target)
    target = replace(target, context_window=reference.prepared_context.fit_ledger.rendered_input_tokens + 10)
    seen = []

    async def dispatch(selected, prepared, evidence, *, attempt_id, **_kwargs):
        seen.append((selected, prepared.content))
        evidence.mark_accepted()
        settle_batch_write(attempt_id, outcome="accepted")
        return "test", None, selected.identity

    monkeypatch.setattr(thread_execution, "_dispatch_prepared", dispatch)
    result = await thread_execution.execute_claimed_batch(batch, thread_row=row, target=target)
    assert isinstance(result, InboxQueuedInput)
    assert result.receipt.message_id == "second"
    assert result.receipt.input_order == batch.items[-1].input_order
    assert [item.message_id for item in seen[0][0].items] == ["first"]
    assert "second" not in seen[0][1]
    assert [tuple(row) for row in main_db.execute(
        "SELECT id,delivery_state,active_turn_id,seq FROM messages ORDER BY input_order"
    )] == [("first", "delivered", "turn-1", 1), ("second", "queued", None, None)]


@pytest.mark.asyncio
async def test_idle_prefix_does_not_split_off_passive_context_only(main_db):
    from infra.input_render import prepare_claimed_batch
    from infra.thread_execution import _prepare_batch
    from llm.resolution import resolve_execution_target

    batch, row = _claim_pair(main_db, passive=True)
    target = resolve_execution_target(batch.target_model, db=main_db)
    reference = await prepare_claimed_batch(replace(batch, items=batch.items[:1]), thread_row=row, target=target)
    target = replace(target, context_window=reference.prepared_context.fit_ledger.rendered_input_tokens + 10)
    prepared = await _prepare_batch(batch, thread_row=row, target=target)
    assert prepared.batch == batch
    assert "first" in prepared.content and "second" in prepared.content
    assert prepared.prepared_context.fit_ledger.rendered_input_tokens > target.context_window
