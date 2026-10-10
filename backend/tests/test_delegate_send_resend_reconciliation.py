"""A resend after a lost result is reconciled by the send's owner, not by Plans.

Turn terminal places no Plans hold on selected work when a tool call ends
without a settled result, so the resend reaches the one owner that can
recognise an already-committed ``delegate_send``: thread input admission. The
send's identity is the durable ``messages.id`` (the caller's
``idempotency_key`` for a progress send, the server-bound
``parent-completion:<child Turn>`` for a completion return), and a resend with
the same identity returns the original receipt with ``duplicate`` true and
admits nothing.

The recovery is conditional. A progress send without ``idempotency_key`` has no
identity for the owner to reconcile, and a completion resent from a later child
Turn carries a new key; neither is covered here because neither is recovered.
"""

from __future__ import annotations

import asyncio
import json

from infra import send_service, thread_driver
from infra.database import get_db
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import execute_as, open_turn, selected_plan
from tools import delegation as delegation_tools
from tools.delegate_send import DelegateSendTool
from tools.registry import ToolRegistry

_ORDER = "Confirm purchase order PO-7: 40 units delivered by Friday."
_ORDER_KEY = "resend-supplier-po-7"


class _ResultLost:
    """Commit the real send, then never return its receipt to the caller."""

    def __init__(self) -> None:
        self.committed = asyncio.Event()
        self._never = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        await send_service.send_thread_input(*args, **kwargs)
        self.committed.set()
        await self._never.wait()


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(DelegateSendTool())
    return registry


def _keep_busy(db, thread_id: str) -> None:
    """Give the recipient a running Turn so accepted input queues, unexecuted."""
    open_turn(db, thread_id, f"{thread_id}-busy")


def _admitted(db, thread_id: str) -> list[str]:
    """Every model-bound input the recipient's owner has accepted."""
    rows = db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND role='user' "
        "ORDER BY input_order",
        (thread_id,),
    ).fetchall()
    return [row["content"] for row in rows]


async def _until_committed(lost: _ResultLost, in_flight: asyncio.Task) -> None:
    waiter = asyncio.ensure_future(lost.committed.wait())
    try:
        await asyncio.wait(
            {waiter, in_flight}, timeout=10, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        waiter.cancel()
    detail = in_flight.result() if in_flight.done() else "send never committed"
    assert lost.committed.is_set(), detail


async def test_resend_after_turn_died_mid_send_is_reconciled_by_the_input_owner(
    isolated_db, monkeypatch,
):
    """The send commits, its Turn dies before the result returns, and the retry
    Turn resends the identical keyed call: the owner answers with the original
    receipt and the supplier holds exactly one order."""
    executor = mk_session("resend-executor")
    supplier = mk_session("resend-supplier")
    db = get_db()
    _keep_busy(db, supplier)
    selected_plan(db, executor)
    registry = _registry()
    params = {
        "thread_id": supplier, "message": _ORDER, "idempotency_key": _ORDER_KEY,
    }
    lost = _ResultLost()
    monkeypatch.setattr(delegation_tools, "send_thread_input", lost)
    first_turn = open_turn(db, executor, "resend-turn-1")
    in_flight = asyncio.create_task(
        execute_as(registry, first_turn, executor, "delegate_send", params),
    )
    try:
        await _until_committed(lost, in_flight)
        thread_driver.finish_turn(
            "resend-turn-1", outcome="error", ended_at="t-lost",
            successor_turn_id="resend-successor", boot_epoch="boot-after-restart",
            error="backend restarted while Turn was running", db=db,
        )
        monkeypatch.setattr(
            delegation_tools, "send_thread_input", send_service.send_thread_input,
        )
        retry_turn = open_turn(db, executor, "resend-turn-2")

        resent = await execute_as(
            registry, retry_turn, executor, "delegate_send", params,
        )

        assert resent.startswith("{"), resent
        receipt = json.loads(resent)
        assert receipt["duplicate"] is True
        assert receipt["queued"]["message_id"] == _ORDER_KEY
        assert _admitted(db, supplier) == [_ORDER]
    finally:
        in_flight.cancel()
        await asyncio.gather(in_flight, return_exceptions=True)
        db.close()


async def test_completion_resent_in_the_same_child_turn_is_one_parent_input(
    isolated_db,
):
    """A child's completion return is keyed by its running Turn, so a
    retry after the first result was lost in transport admits nothing new."""
    parent = mk_session("resend-parent")
    child = mk_session("resend-child", spawned_by=parent)
    db = get_db()
    _keep_busy(db, parent)
    selected_plan(db, child)
    child_turn = open_turn(db, child, "resend-child-turn")
    registry = _registry()
    params = {
        "thread_id": parent, "message": "Supplier confirmed PO-7.",
        "intent": "completion",
    }
    try:
        await execute_as(registry, child_turn, child, "delegate_send", params)

        resent = await execute_as(registry, child_turn, child, "delegate_send", params)

        receipt = json.loads(resent)
        assert receipt["completion"] == "accepted"
        assert receipt["duplicate"] is True
        assert receipt["queued"]["message_id"] == "parent-completion:resend-child-turn"
        assert _admitted(db, parent) == ["Supplier confirmed PO-7."]
    finally:
        db.close()


async def test_keyed_resend_with_a_reworded_body_is_refused_not_admitted(
    isolated_db,
):
    """The key binds the exact payload, so a reworded retry is refused
    by the owner instead of becoming a second order."""
    executor = mk_session("resend-reword-executor")
    supplier = mk_session("resend-reword-supplier")
    db = get_db()
    _keep_busy(db, supplier)
    turn = open_turn(db, executor, "resend-reword-turn")
    registry = _registry()
    params = {
        "thread_id": supplier, "message": _ORDER, "idempotency_key": _ORDER_KEY,
    }
    try:
        await execute_as(registry, turn, executor, "delegate_send", params)

        reworded = await execute_as(
            registry, turn, executor, "delegate_send",
            params | {"message": "Please confirm PO-7 (40 units, Friday)."},
        )

        assert reworded.startswith("Error executing delegate_send:"), reworded
        assert "input identity already has a different payload" in reworded
        assert _admitted(db, supplier) == [_ORDER]
    finally:
        db.close()
