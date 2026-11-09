"""A Codex terminal drives its thread outside the event chain it arrived in.

A steer holds the thread's transition lock while it drains the provider events
deferred behind its input (``release_delivery_event_barrier``). A terminal
among them must not dispatch the thread's next Turn inside that chain: the
dispatch takes the same lock, so the drain and the dispatch wait on each other
and the next Turn stays ``running`` with no driver (#1981). Both of a
terminal's own drives take it: the queued successor its settlement claims, and
the unanswered-input wake an errored Turn queues for its own thread.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from codex_runtime_fixtures import _active_live, _make_mock_app_session, _reset_runtime

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

THREAD = "codex-drained-terminal"
HANG_GUARD_SECONDS = 30


@pytest.fixture(autouse=True)
def _isolated_runtime():
    _reset_runtime()
    yield
    _reset_runtime()


def _queue(message_id: str) -> None:
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    admission = admit_input(
        THREAD, role="user", content=message_id, intent="apply_now",
        message_id=message_id, turn_id=f"turn-{message_id}", boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
    )
    assert admission.batch is None  # the writer's Turn is running, so it waits in the FIFO


def _delivery_state(where: str, *args: str) -> str:
    from infra.database import get_db

    db = get_db()
    try:
        return str(db.execute(
            f"SELECT delivery_state FROM messages WHERE {where}", args,
        ).fetchone()[0])
    finally:
        db.close()


async def _drain_terminal_under_the_steer_lock(live, terminal: dict):
    """Deliver ``terminal`` behind a steer's barrier, drain it under the lock, then let its drives run."""
    from subagents import codex_runtime
    from subagents.codex_runtime_event_delivery import (
        arm_delivery_event_barrier,
        release_delivery_event_barrier,
    )
    from subagents.codex_runtime_state import drain_detached_drives

    on_event = codex_runtime._make_on_event(live, THREAD, MagicMock())
    fresh = _make_mock_app_session("provider-writer")
    conn = {"status": "available", "config": {"binary": "codex", "app_server": True}}
    with (
        patch("connections.models.get_connection", return_value=conn),
        patch("subagents.codex_app_server.CodexAppServerSession", return_value=fresh),
    ):
        async with codex_runtime._LIVE.lock(THREAD):  # the steer's hold
            arm_delivery_event_barrier(live)
            await on_event({**terminal, "is_current_turn": True})
            drain = asyncio.create_task(release_delivery_event_barrier(live))
            # Not wait_for: the drain shields itself from cancellation by design.
            done, _ = await asyncio.wait({drain}, timeout=HANG_GUARD_SECONDS)
            if not done:
                live.delivery_event_tail.cancel()  # unwind the deadlock so the test can end
                await asyncio.gather(drain, return_exceptions=True)
        assert done, "the drained terminal waited on the transition lock its drainer holds"
        await drain_detached_drives()
    return fresh


@pytest.mark.asyncio
async def test_result_drained_under_the_transition_lock_drives_its_successor_after(
    isolated_db,
) -> None:
    live = _active_live(THREAD, _make_mock_app_session("provider-writer"), pending_tool=False)
    _queue("input-behind-result")

    fresh = await _drain_terminal_under_the_steer_lock(live, {"kind": "result", "is_error": False})

    assert _delivery_state("id=?", "input-behind-result") == "delivered"
    assert fresh.start_turn.await_count + fresh.start.await_count == 1


@pytest.mark.asyncio
async def test_errored_result_drained_under_the_transition_lock_drives_its_wake_after(
    isolated_db,
) -> None:
    from infra.unanswered_input_wake import EVENT

    # The Turn took in the person's input and errored before answering it.
    live = _active_live(THREAD, _make_mock_app_session("provider-writer"), pending_tool=False)

    fresh = await _drain_terminal_under_the_steer_lock(
        live, {"kind": "result", "is_error": True, "result": "Codex turn failed"},
    )

    assert _delivery_state(
        "thread_id=? AND json_extract(metadata,'$.event_type')=?", THREAD, EVENT,
    ) == "delivered"
    assert fresh.start_turn.await_count + fresh.start.await_count == 1


def test_drain_waits_only_for_drives_on_its_own_loop() -> None:
    from subagents.codex_runtime_state import (
        _DETACHED_DRIVES, drain_detached_drives, schedule_detached_drive,
    )

    # An earlier loop closed with its drive still pending: it can never finish.
    earlier = asyncio.new_event_loop()

    async def leave_a_drive_pending() -> asyncio.Task[None] | None:
        return schedule_detached_drive(asyncio.sleep(HANG_GUARD_SECONDS), name="stranded")

    try:
        stranded = earlier.run_until_complete(leave_a_drive_pending())
    finally:
        earlier.close()
    assert stranded is not None and not stranded.done()

    drove: list[str] = []

    async def drive() -> None:
        drove.append("own")

    async def drain_own() -> None:
        schedule_detached_drive(drive(), name="own")
        await asyncio.wait_for(drain_detached_drives(), HANG_GUARD_SECONDS)

    try:
        asyncio.run(drain_own())
    finally:
        _DETACHED_DRIVES.discard(stranded)
    assert drove == ["own"]
