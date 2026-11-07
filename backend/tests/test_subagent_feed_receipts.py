"""Worker direct receipts retain final delivery state and acknowledge holds early."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from infra.send_types import InboxQueuedInput, InputReceipt, SendResult
from ws.subagent_control import _deliver_subagent_input


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True])
async def test_worker_publishes_exactly_one_receipt_at_its_authoritative_boundary(held):
    socket = SimpleNamespace(send_json=AsyncMock())
    queued = InboxQueuedInput(
        InputReceipt("child", "message", 1, 1, "2026-11-06T00:00:00"),
        admission_hold="usage_limit" if held else None,
    )
    final = queued if held else SendResult("codex_sub", message_id="message", turn_id="turn", turn_order=1)

    async def send(*args, on_admitted, **kwargs):
        await on_admitted(queued)
        assert socket.send_json.await_count == int(held)
        return final

    error = await _deliver_subagent_input(
        socket, "child", object(), "continue", None, False, "input",
        "apply_now", send, "Codex", Mock(), None, None,
    )

    assert error is None
    socket.send_json.assert_awaited_once()
    assert socket.send_json.await_args.args[0]["type"] == (
        "input_queued" if held else "input_accepted"
    )
