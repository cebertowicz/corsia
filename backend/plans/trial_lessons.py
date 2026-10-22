"""Attach a started task's trial lessons to a plan result or wake."""
from __future__ import annotations

import sqlite3

from loguru import logger as log


def attach_trial_lessons(
    db: sqlite3.Connection, facts: dict, *, thread_id: str, node_id: str,
    checkpoint_generation_id: str | None = None,
) -> None:
    """Add lessons only when Plans still attributes this exact task to the caller."""
    from core.lesson_delivery import start_trials
    from plans.selection_validity import attributed_selection

    selected = attributed_selection(db, thread_id)
    if selected is None or str(selected["node_id"]) != node_id:
        return
    trials = start_trials(
        db, thread_id=thread_id, node_id=node_id,
        checkpoint_generation_id=checkpoint_generation_id,
    )
    if trials:
        facts["trial_lessons"] = trials


def attach_trial_lessons_advisory(
    db: sqlite3.Connection,
    facts: dict,
    *,
    thread_id: str,
    node_id: str,
    checkpoint_generation_id: str | None = None,
) -> None:
    """Attach advisory trial lessons without changing the plan operation's outcome."""
    try:
        attach_trial_lessons(
            db,
            facts,
            thread_id=thread_id,
            node_id=node_id,
            checkpoint_generation_id=checkpoint_generation_id,
        )
    except Exception as exc:  # noqa: BLE001 - advisory sibling never fails a plan mutation
        log.exception("trial delivery unavailable for selected node %s: %s", node_id, exc)
