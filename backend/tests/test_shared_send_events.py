from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from core.turn_core import TurnIdentity
from shared_send_fixtures import (
    _insert_session,
    _install_fake_native_context_agent,
    _messages,
    _thread_row,
)

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


@pytest.fixture(autouse=True)
def _native_context_agent(monkeypatch):
    _install_fake_native_context_agent(monkeypatch)


@pytest.mark.asyncio
async def test_routed_events_are_idempotent_on_stable_event_payload(isolated_db):
    _insert_session(
        "cx-main-idem",
        user_id="user-1",
        model="codex/gpt-5-codex",
        spawned_by=None,
    )
    from events.delivery import deliver_to_thread
    from subagents import codex_runtime

    async def _feed_main(*_args, **kwargs):
        turn_id = kwargs["successor_turn_id"]
        await kwargs["bind_input_turn"](turn_id, None)
        kwargs["mark_accepted"]()
        return TurnIdentity(turn_id, 1)

    feed_main = AsyncMock(side_effect=_feed_main)
    details = {
        "repo": "o/r",
        "external_id": "o/r#1",
        "url": "https://example.test/pr/1",
    }
    with (
        patch.object(
            codex_runtime, "ensure_connection_available", lambda db=None: None
        ),
        patch.object(codex_runtime, "has_thread_id", AsyncMock(return_value=False)),
        patch.object(
            codex_runtime, "has_prior_messages", AsyncMock(return_value=False)
        ),
        patch.object(codex_runtime, "feed_main", feed_main),
    ):
        first = await deliver_to_thread(
            "cx-main-idem", "webhook.github.pr_opened", "o/r#1 opened", details
        )
        second = await deliver_to_thread(
            "cx-main-idem", "webhook.github.pr_opened", "o/r#1 opened", details
        )

    assert first is True
    assert second is True
    feed_main.assert_awaited_once()
    rows = _messages("cx-main-idem")
    chips = [
        row
        for row in rows
        if row["role"] == "system"
        and json.loads(row["metadata"]).get("routed_event")
    ]
    assert len(chips) == 1


@pytest.mark.asyncio
async def test_routed_events_distinguish_github_delivery_instances(isolated_db):
    _insert_session(
        "cx-main-delivery-id",
        user_id="user-1",
        model="codex/gpt-5-codex",
        spawned_by=None,
    )
    from events.delivery import deliver_to_thread
    from subagents import codex_runtime

    async def _feed_main(*_args, **kwargs):
        turn_id = kwargs["successor_turn_id"]
        await kwargs["bind_input_turn"](turn_id, None)
        kwargs["mark_accepted"]()
        return TurnIdentity(turn_id, 1)

    feed_main = AsyncMock(side_effect=_feed_main)
    details = {
        "repo": "o/r",
        "external_id": "o/r#1140",
        "check_suite_id": 41,
        "head_sha": "head-a",
    }
    with (
        patch.object(
            codex_runtime, "ensure_connection_available", lambda db=None: None
        ),
        patch.object(codex_runtime, "has_thread_id", AsyncMock(return_value=False)),
        patch.object(
            codex_runtime, "has_prior_messages", AsyncMock(return_value=False)
        ),
        patch.object(codex_runtime, "feed_main", feed_main),
    ):
        replay_first = await deliver_to_thread(
            "cx-main-delivery-id",
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            details,
            delivery_id="delivery-1",
        )
        replay_duplicate = await deliver_to_thread(
            "cx-main-delivery-id",
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            details,
            delivery_id="delivery-1",
        )
        rerun = await deliver_to_thread(
            "cx-main-delivery-id",
            "webhook.github.check_suite_completed",
            "check suite succeeded",
            details,
            delivery_id="delivery-2",
        )

    assert replay_first is True
    assert replay_duplicate is True
    assert rerun is True
    feed_main.assert_awaited_once()
    rows = _messages("cx-main-delivery-id")
    chips = [
        row
        for row in rows
        if row["role"] == "system"
        and json.loads(row["metadata"]).get("routed_event")
    ]
    assert len(chips) == 2


@pytest.mark.asyncio
async def test_native_routed_event_binds_turn_then_persists_before_inference(
    isolated_db,
):
    _insert_session(
        "native-event",
        user_id="user-1",
        model="opus",
        spawned_by=None,
    )
    from events.delivery import deliver_to_thread

    async def _ok(*args, **kwargs):
        await kwargs["persist_input_once"]()
        kwargs["start_ack"].set_result(TurnIdentity(kwargs["turn_id"], 1))
        return None

    with patch("ws.turn.execute_turn", side_effect=_ok) as execute_turn:
        ok = await deliver_to_thread(
            "native-event", "webhook.github.pr_opened", "o/r#2 opened", {"x": 1}
        )

    assert ok is True
    execute_turn.assert_awaited_once()
    assert execute_turn.await_args.kwargs["ingress"] == "event"
    assert execute_turn.await_args.kwargs["step_metadata"]["routed_event"] == (
        "webhook.github.pr_opened"
    )
    event_rows = [
        row for row in _messages("native-event")
        if row["role"] == "system"
        and json.loads(row["metadata"]).get("routed_event")
    ]
    assert len(event_rows) == 1
    assert event_rows[0]["content"].startswith("[EVENT: webhook.github.pr_opened]")
    assert json.loads(event_rows[0]["metadata"])["routed_event"] == (
        "webhook.github.pr_opened"
    )


@pytest.mark.asyncio
async def test_native_event_failure_after_turn_acceptance_preserves_input(
    isolated_db,
):
    _insert_session(
        "native-event-accepted-error",
        user_id="user-1",
        model="opus",
        spawned_by=None,
    )
    from infra.send_service import send_thread_input

    async def _accepted_then_failed(
        *_args,
        start_ack=None,
        turn_id=None,
        persist_input_once=None,
        preadmitted_turn=None,
        provider_acceptance=None,
        **_kwargs,
    ):
        assert start_ack is not None
        assert persist_input_once is not None
        await persist_input_once()
        assert preadmitted_turn is not None
        start_ack.set_result(preadmitted_turn)
        provider_acceptance.set_result(True)
        raise RuntimeError("failure after native acceptance")

    with patch("ws.turn.execute_turn", side_effect=_accepted_then_failed):
        result = await send_thread_input(
            "native-event-accepted-error",
            _thread_row("native-event-accepted-error"),
            "accepted input",
            native_mode="event",
        )

    assert result.driver == "turn_engine"

    user_rows = [
        row for row in _messages("native-event-accepted-error") if row["role"] == "user"
    ]
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == "accepted input"


@pytest.mark.asyncio
async def test_native_event_request_cancel_after_acceptance_preserves_input(
    isolated_db,
):
    _insert_session(
        "native-event-accepted-cancel",
        user_id="user-1",
        model="opus",
        spawned_by=None,
    )
    from infra.send_service import send_thread_input

    accepted = asyncio.Event()

    async def _accepted_then_blocked(
        *_args,
        start_ack=None,
        turn_id=None,
        persist_input_once=None,
        preadmitted_turn=None,
        **_kwargs,
    ):
        assert start_ack is not None
        assert persist_input_once is not None
        await persist_input_once()
        start_ack.set_result(preadmitted_turn)
        accepted.set()
        await asyncio.Event().wait()

    with patch("ws.turn.execute_turn", side_effect=_accepted_then_blocked):
        task = asyncio.create_task(
            send_thread_input(
                "native-event-accepted-cancel",
                _thread_row("native-event-accepted-cancel"),
                "accepted before cancellation",
                native_mode="event",
            )
        )
        await accepted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    user_rows = [
        row
        for row in _messages("native-event-accepted-cancel")
        if row["role"] == "user"
    ]
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == "accepted before cancellation"


@pytest.mark.asyncio
async def test_routed_event_rejects_user_archive_without_row_or_dispatch(isolated_db):
    _insert_session(
        "archived-event-target",
        user_id="user-1",
        model="codex/gpt-5-codex",
        spawned_by=None,
        lifecycle_state="archived",
        archived_reason="user",
    )
    from events.delivery import deliver_to_thread
    from subagents import codex_runtime

    feed_main = AsyncMock(return_value=TurnIdentity("never", 1))
    with patch.object(codex_runtime, "feed_main", feed_main):
        accepted = await deliver_to_thread(
            "archived-event-target", "webhook.github.pr_opened", "summary", None
        )
    assert accepted is False
    feed_main.assert_not_awaited()
    assert _messages("archived-event-target") == []
