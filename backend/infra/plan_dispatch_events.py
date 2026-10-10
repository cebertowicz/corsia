"""The ``plan_dispatch`` product events, always emitted off the event loop.

A product event is a synchronous DuckDB write, so nothing here runs on the
event loop. The tool boundary (``tools.dispatch_ownership``, reached from
``tools.recorded_call``) calls the ``emit_*``
functions inside the database worker, right after the dispatch write each one
reports commits or fails. The Turn-terminal settlement driver awaits
``report_terminal_unknown``, which hops to that worker only when the committed
terminal settled a call as unknown.

Events carry tool names, reasons, outcomes and counts only; never a tool's
arguments or result. ``plans.tool_dispatches`` rows stay the state.
"""

from __future__ import annotations

from infra import product_events
from infra.db_async import run_in_db


def emit_recorded(thread_id: str, tool: str) -> None:
    """Report a call recorded under the executor's valid selection."""
    product_events.emit_product_event(
        "plan_dispatch", "recorded", thread_id=thread_id, properties={"tool": tool},
    )


def emit_unrecorded(thread_id: str, tool: str, reason: str) -> None:
    """Report a call that ran without a dispatch row, and why."""
    product_events.emit_product_event(
        "plan_dispatch", "unrecorded", thread_id=thread_id,
        properties={"tool": tool, "reason": reason},
    )


def emit_record_failed(thread_id: str, tool: str) -> None:
    """Report a recording failure that kept the call from starting."""
    product_events.emit_product_event(
        "plan_dispatch", "record_failed", thread_id=thread_id,
        properties={"tool": tool},
    )


def emit_settled(thread_id: str, tool: str, outcome: str) -> None:
    """Report a started call that settled its own row."""
    product_events.emit_product_event(
        "plan_dispatch", "settled", thread_id=thread_id,
        properties={"tool": tool, "outcome": outcome},
    )


def emit_settle_failed(thread_id: str, tool: str, outcome: str) -> None:
    """Report a started call whose settlement write failed."""
    product_events.emit_product_event(
        "plan_dispatch", "settle_failed", thread_id=thread_id,
        properties={"tool": tool, "outcome": outcome},
    )


def _emit_terminal_unknown(thread_id: str | None, count: int) -> None:
    product_events.emit_product_event(
        "plan_dispatch", "terminal_unknown", thread_id=thread_id,
        properties={"count": count},
    )


async def report_terminal_unknown(settlement) -> None:
    """Report recorded calls a committed Turn terminal settled as unknown, if any."""
    if not settlement.plan_dispatches_unknown:
        return
    await run_in_db(
        _emit_terminal_unknown, settlement.thread_id, settlement.plan_dispatches_unknown,
    )
