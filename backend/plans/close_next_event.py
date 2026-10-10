"""The content-free evidence one closing result leaves in the analytics store.

Split from ``plans.close_next``, which composes the result an agent reads: this
reads a composed result and returns codes and counts about it, never the block
itself or the description beside it. This module emits that evidence once the
close has committed.
"""

from __future__ import annotations

import logging
from typing import Any

from plans import handoff_brief
from plans.close_next import STARTED
from prompts import plan_tools as text

logger = logging.getLogger(__name__)

#: What the description properties say when the fact beside ``next`` could not
#: be measured: unknown, never a claim that nothing was carried.
UNMEASURED = {"description_carried": None, "description_bytes": None, "description_cut": None}


def close_event_properties(operation: str, result: dict, actor) -> dict[str, Any]:
    """Return the content-free evidence one closing result carries: no ids or text.

    ``result`` is the whole closing result: its ``next`` block, and the
    description beside it when the close handed one over. What that
    description cost is read from the fact itself (``handoff_brief.measured``).
    A fact that cannot be measured costs its own three properties, never the
    rest of the event, and is logged: only a malformed fact reaches it.
    """
    block = result["next"]
    try:
        described = handoff_brief.measured(result)
    except (KeyError, TypeError) as exc:
        logger.warning(
            "close_next description unmeasured for %s: %r", operation, exc, exc_info=True,
        )
        described = UNMEASURED
    named = block.get("named")
    selected = block["selected"]
    shown, total = text.listed_counts(block["open_tasks_listed"])
    idle = block["open_tasks_listed"] == text.idle_listed_text(shown, total)
    return {
        "idle": (
            "pick_up" if "idle" in block else "imminent" if idle else "none"
        ),
        "operation": operation,
        "named": (
            "absent" if named is None
            else STARTED if named["started"] else named["refusal"]
        ),
        "selected_how": "none" if selected is None else selected["how"],
        "open_total": total,
        "open_listed": shown,
        "executor_is_actor": block["executor_thread_id"] == actor.thread_id,
        **described,
    }


def emit_close_event(operation: str, result: dict, actor) -> None:
    """Emit close evidence after the write is durable without affecting its result."""
    from infra import onboarding_events
    from infra.product_events import emit_product_event

    try:
        emit_product_event(
            "plans",
            "close_next",
            thread_id=actor.thread_id,
            properties=close_event_properties(operation, result, actor),
        )
    except Exception:  # noqa: BLE001 - evidence may never break a committed close
        logger.warning("plans close_next emit failed for %s", operation, exc_info=True)
    onboarding_events.note_close(operation, actor.project_id)
