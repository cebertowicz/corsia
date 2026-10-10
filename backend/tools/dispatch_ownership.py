"""Plan-dispatch ownership for one started registry tool call.

``plans.tool_dispatches`` owns the dispatch rows; this module is the tool
boundary's side of that contract, reached from ``tools.recorded_call``. A call
that starts under the executor's valid selection is recorded before the tool
runs and settled when it ends, so every started effect stays attributed to
the attempt it ran under. Plans' own tools (tool catalog category ``plans``)
run without a row, because whatever they change commits inside the Plans write
itself.

Recording failures are never swallowed: a recorder failure stops the call
before the tool runs. A settlement failure after a failed call becomes the
call's error; after an interruption or a completed call it is logged and does
not replace the outcome the caller already holds (``settle_interrupted_call``,
``settle_completed_call``).

Each outcome is also reported as a ``plan_dispatch`` product event through
``infra.plan_dispatch_events``, in the same database worker right after the
write it describes commits or fails, so no analytics write runs on the event
loop.

``plans.tool_dispatches`` is resolved at call time, so its functions stay
replaceable module attributes.
"""

from __future__ import annotations

import logging
from typing import Any

from infra import plan_dispatch_events
from infra.tool_result_errors import is_error_result

log = logging.getLogger(__name__)


def _id_prefix(value: str | None) -> str:
    """Return an identity's 8-character prefix, or ``none`` when it is absent."""
    return value[:8] if isinstance(value, str) and value else "none"


def is_plans_catalog_tool(name: str) -> bool:
    """Return whether the tool catalog classifies ``name`` as one of Plans' own tools.

    This is the one Plans-tool classifier: the plan-upkeep notice
    (``plans.upkeep_notice.is_plans_tool``) delegates here. A harness prefix
    such as ``mcp__corsia__`` is dropped first.

    Whatever a catalog ``plans`` tool changes commits inside the Plans write
    itself, so no effect of its call can still land after that write and it
    needs no dispatch row. The catalog imports the registry, which imports
    this module, so the catalog is resolved here at call time.
    """
    from tools.catalog import TOOL_CATALOG, normalize_tool_name

    entry = TOOL_CATALOG.get(normalize_tool_name(name))
    return entry is not None and entry.category == "plans"


async def record_dispatch(
    name: str, thread_id: str, turn_id: str | None, dispatch_id: str,
) -> bool:
    """Record a starting call under the thread's selected plan work.

    No plan state makes this refuse: without a live Turn or a valid selection
    it records nothing and the call runs. A recorder failure is different: the
    call must not start without the ownership record that lets a concurrent
    release wait for its effect, so the failure is logged and propagates, and
    the caller settles any row it may have written.

    Returns:
        Whether a dispatch row was written.
    """
    from infra.db_async import run_in_db

    try:
        return await run_in_db(
            _record_and_report, thread_id, turn_id, name, dispatch_id,
        )
    except Exception:
        log.exception(
            "plan dispatch recording failed; call not started: tool=%s thread=%s",
            name, thread_id,
        )
        raise


def _record_and_report(
    thread_id: str, turn_id: str | None, name: str, dispatch_id: str,
) -> bool:
    """Record one starting call, then report whether a row was written."""
    from plans import public

    try:
        written = public.record_tool_dispatch(
            thread_id, turn_id, name, dispatch_id=dispatch_id,
        )
    except Exception:
        plan_dispatch_events.emit_record_failed(thread_id, name)
        raise
    if written:
        plan_dispatch_events.emit_recorded(thread_id, name)
    else:
        reason = "no_turn" if turn_id is None else "no_selection"
        plan_dispatch_events.emit_unrecorded(thread_id, name, reason)
    return written


def _settle_and_report(
    thread_id: str, name: str, dispatch_id: str, outcome: str,
) -> None:
    """Settle one started call's row by id, then report a settlement it made."""
    from plans import public

    try:
        settled = public.settle_tool_dispatch(dispatch_id, outcome)
    except Exception:
        plan_dispatch_events.emit_settle_failed(thread_id, name, outcome)
        raise
    if settled:
        plan_dispatch_events.emit_settled(thread_id, name, outcome)


async def _settle_dispatch(
    thread_id: str, name: str, dispatch_id: str, outcome: str,
) -> None:
    """Settle one started call's dispatch row by id; a failure propagates."""
    from infra.db_async import run_in_db

    await run_in_db(_settle_and_report, thread_id, name, dispatch_id, outcome)


async def settle_completed_call(
    name: str, thread_id: str, dispatch_id: str, result: Any,
    turn_id: str | None,
) -> None:
    """Settle a call that returned, as refused or succeeded.

    The call's effect has already landed, so its settlement is accounting and no
    settlement failure, of any class, fails the Turn: the failure is logged with
    its traceback and the open row is left to Turn-terminal settlement
    (``plans.tool_dispatches.settle_turn``). A cancellation is not an ``Exception``
    and propagates unchanged.

    The open row closes as ``unknown`` when the Turn ends (counted in
    ``plan_dispatch.terminal_unknown``). It holds no slot meanwhile: parking
    the task releases its attempt regardless (#2705).
    """
    refused = is_error_result(result)
    outcome = "refused" if refused else "succeeded"
    try:
        await _settle_dispatch(thread_id, name, dispatch_id, outcome)
    except Exception:
        log.exception(
            "plan dispatch settlement failed after a completed call; the row is "
            "left to Turn-terminal settlement: tool=%s call=%s turn=%s "
            "outcome=%s",
            name, _id_prefix(dispatch_id), _id_prefix(turn_id), outcome,
        )


async def settle_failed_call(
    name: str, thread_id: str, dispatch_id: str, call_error: Exception,
) -> None:
    """Settle a call that raised, or whose recording raised, as failed.

    Raises:
        RuntimeError: Settlement failed too (logged first). The message names
            both failures and is chained to the call's own error.
    """
    try:
        await _settle_dispatch(thread_id, name, dispatch_id, "failed")
    except Exception as settle_error:
        log.exception(
            "plan dispatch settlement failed after the call failed: "
            "tool=%s dispatch=%s",
            name, dispatch_id,
        )
        raise RuntimeError(
            f"{name} failed: {call_error}; its plan dispatch could not be "
            f"settled either: {settle_error}"
        ) from call_error


async def settle_interrupted_call(
    name: str, thread_id: str, dispatch_id: str,
) -> None:
    """Settle a cancelled or otherwise interrupted call as unknown.

    The caller re-raises the interruption, which a settlement failure must not
    replace, so that failure is logged here and not raised.
    """
    try:
        await _settle_dispatch(thread_id, name, dispatch_id, "unknown")
    except Exception:
        log.exception(
            "plan dispatch settlement failed after the call was interrupted; "
            "the interruption propagates: tool=%s dispatch=%s",
            name, dispatch_id,
        )
