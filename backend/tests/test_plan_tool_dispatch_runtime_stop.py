"""At the runtime Stop boundary, a recorded call is stopped cleanly.

Entry is the requester Stop owner, `ws.stop_control.do_stop`, for a harness
Turn whose backend tool call is running through the real `ToolRegistry`. The
harness Stop keeps Claude Code's exact order from `subagents.cc_stop`: halt the
exact live with the real `cc_runtime_quiescence.halt_exact_live_before_transition`
(it revokes tool access and cancels the admitted tool tasks), join those tasks
with the real `join_admitted_tool_tasks`, then commit the Turn terminal through
the canonical `core.turn_core.settle_turn_terminal`. Only the Claude Code CLI
session and OS process kill are absent in-process, as in `test_clean_stop.py`.

Why not a Native Turn: Native Stop does not cancel a running tool; it cancels
provider I/O, kills owned processes, and joins the Turn, so the call settles
with the tool's own result. Why not a terminal committed while the call is
still open: no runtime commits a Stop terminal before its admitted tool tasks
quiesce (`_close_and_finalize` refuses to finalize otherwise). That ordering is
Turn-terminal recovery, proven through `finish_turn` in
`test_plan_turn_terminal_settlement.py` and `test_plan_turn_terminal_recovery.py`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.turn_core import TurnIdentity, settle_turn_terminal
from infra import state
from infra.database import get_db
from plans import admission
from subagents import cc_runtime, codex_runtime
from subagents import cc_runtime_quiescence as quiescence
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    BlockingProbe,
    abandon_blocking_call,
    dispatch_rows,
    open_attempt_ids,
    open_turn,
    registry_of,
    selected_plan,
    start_blocking_call,
)
from ws.stop_control import do_stop

_USER = {"id": "user-1"}


class _CliSession:
    """The Claude Code CLI process handle, which has no in-process process."""

    mcp_config = None

    def _request_terminate(self) -> None:
        return None

    async def terminate(self) -> None:
        return None


class _ExactLive:
    """The harness live registry holding one exact live handle."""

    def __init__(self, live) -> None:
        self._live = live

    def get(self, _thread_id):
        return self._live


class _HarnessStop:
    """Claude Code's exact Stop order over the real CORSIA-owned helpers."""

    def __init__(self, live) -> None:
        self._live = live

    async def __call__(self, thread_id: str, *, expected_turn_id: str, **_kwargs) -> bool:
        admitted = tuple(self._live.admitted_tool_tasks)
        halted = await quiescence.halt_exact_live_before_transition(
            thread_id, expected_turn_id,
        )
        deadline = asyncio.get_running_loop().time() + 5
        quiesced = await quiescence.join_admitted_tool_tasks(
            self._live, admitted, deadline=deadline,
        )
        if not (halted and quiesced):
            return False
        await settle_turn_terminal(expected_turn_id, "cancelled")
        return True


async def _no_owned_processes(*_args, **_kwargs) -> None:
    return None


async def _owns_thread(_user, _thread_id) -> bool:
    return True


def _ignore_broadcast(*_args) -> None:
    return None


def _owner_broadcast(_user_id):
    return _ignore_broadcast


def _harness_live(identity: TurnIdentity, registry) -> SimpleNamespace:
    """One exact harness live whose backend tool calls are admitted tasks."""
    return SimpleNamespace(
        corsia_turn=identity,
        provider_context_untrusted=False,
        tool_token="",
        tool_config="",
        tool_registry=registry,
        admitted_tool_tasks=set(),
        session=_CliSession(),
    )


def _install_harness_stop(monkeypatch, live) -> None:
    from host_access.processes import PROCESS_OWNER

    monkeypatch.setattr(quiescence, "_LIVE", _ExactLive(live))
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", _no_owned_processes)
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", _harness_owns_the_turn)
    monkeypatch.setattr(cc_runtime, "stop", _HarnessStop(live))
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", _no_codex_turn)
    monkeypatch.setattr(state, "owner_broadcast", _owner_broadcast)


def _harness_owns_the_turn(_thread_id) -> bool:
    return True


def _no_codex_turn(_thread_id) -> bool:
    return False


async def test_stop_during_a_recorded_call_settles_it_unknown_and_keeps_the_selection(
    isolated_db, monkeypatch,
):
    """Stop wins, the call closes once, and the work stays selected.

    The call is recorded under the selected task and still running when Stop
    arrives. Stop cancels it, its dispatch settles exactly once as `unknown`,
    the Turn ends `cancelled`, no hold is created, and the still-valid
    selection stays running for the next Turn.
    """
    thread_id = mk_session("runtime-stop", model="claude-code/sonnet")
    db = get_db()
    identity = open_turn(db, thread_id, "stopped-turn")
    _plan, selected = selected_plan(db, thread_id)
    attempt_id = selected["admission"]["selection"]["attempt_id"]
    blocking = BlockingProbe()
    registry = registry_of(blocking)
    live = _harness_live(identity, registry)
    _install_harness_stop(monkeypatch, live)
    call = await start_blocking_call(registry, identity, thread_id, blocking)
    live.admitted_tool_tasks.add(call)
    call.add_done_callback(live.admitted_tool_tasks.discard)
    try:
        assert [(row["attempt_id"], row["settled_at"]) for row in dispatch_rows(db)] == [
            (attempt_id, None),
        ]

        receipt = await do_stop(
            _USER, thread_id, None, None, identity.turn_id, owns_thread_fn=_owns_thread,
        )
    finally:
        await abandon_blocking_call(blocking, call)

    assert receipt.outcome == "stopped"
    assert call.cancelled()
    assert db.execute(
        "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()["status"] == "cancelled"
    [dispatch] = dispatch_rows(db)
    assert dispatch["attempt_id"] == attempt_id
    assert dispatch["outcome"] == "unknown"
    assert dispatch["settled_at"] is not None
    assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0
    assert open_attempt_ids(db) == {attempt_id}
    projected = admission.project(selected["node"]["id"], db=db)
    assert projected["condition"] == "running"
    assert projected["selection"]["attempt_id"] == attempt_id
    assert projected["selection"]["in_flight_dispatch_count"] == 0
    db.close()
