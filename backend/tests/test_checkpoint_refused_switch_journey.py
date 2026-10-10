"""A refused input fails visibly; after a model selection, Retry delivers it once."""

import pytest

from journey_fixtures import journeys, assert_idle_clean  # noqa: F401
from tests.contract.matrix_input_recovery import (
    _assert_checkpoint_failure,
    _input_row,
    _unfittable_but_recoverable_content,
    collect_marked_turn,
)
from tests.contract.matrix_runner import TOKEN, TurnDriver, assistant_texts, frame_types

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_selected_destination_delivers_retried_refused_input(journeys):
    thread_id = await journeys.create_session("refused switch exit")
    window = await journeys.seed_pressured_history(thread_id)
    content = _unfittable_but_recoverable_content(window)
    await journeys.seed_replies([])
    async with TurnDriver(journeys.backend) as driver:
        await driver.subscribe(thread_id)
        refused_id = await driver.send(thread_id, content)
        failure = await driver.collect(thread_id, until=("error", "turn_complete"), timeout=30)
        assert "error" in frame_types(failure)
        failed = await _assert_checkpoint_failure(journeys, thread_id, refused_id, content)
        assert await journeys.call_count() == 0
        await journeys.seed_replies([{"content": "RETRIED_ON_SELECTED_MODEL", "finish_reason": "stop"}])
        profile = f"/api/models/mock-test-model/profile?token={TOKEN}"
        await journeys._request("PATCH", profile, json={"context_window": 393_216})
        try:
            await journeys.switch_model(thread_id, "mock/test-model")
            assert _input_row(journeys, thread_id, refused_id) == failed, (
                "selecting a model silently revived a failed input"
            )
            retry = await journeys.message_action(thread_id, refused_id, "retry")
            frames = await collect_marked_turn(driver, thread_id, "RETRIED_ON_SELECTED_MODEL", 30)
        finally:
            await journeys._request("PATCH", profile, json={"context_window": 128_000})
    revived_id = retry.get("revived_message_id")
    assert retry.get("retried") and revived_id and revived_id != refused_id
    assert "RETRIED_ON_SELECTED_MODEL" in assistant_texts(frames)
    assert await journeys.call_count() == 1
    assert (await journeys.session_row(thread_id))["model"] == "mock/test-model"
    revived = _input_row(journeys, thread_id, revived_id)
    assert revived["content"] == failed["content"]
    assert revived["target_model"] == "mock/test-model"
    assert revived["target_execution_epoch"] > failed["target_execution_epoch"]
    assert revived["delivery_state"] == "delivered" and revived["write_outcome"] == "accepted"
    assert _input_row(journeys, thread_id, refused_id) == failed, "Retry rewrote the failed record"
    rows = await journeys.messages(thread_id)
    assert sum(row["id"] == refused_id for row in rows) == 1
    assert sum(row["id"] == revived_id for row in rows) == 1
    await assert_idle_clean(journeys, thread_id)
