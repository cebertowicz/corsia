"""Measure the saved decision named by a successful tool receipt."""

from __future__ import annotations

import json
from typing import Any, Callable

from core.attention_measure import Measure, measure_text


def measure_decision(thread_id: str, arguments: dict, result: Any, get_db: Callable) -> Measure | None:
    """Read the person's decision text through the Plans public boundary."""
    if arguments.get("operation") not in ("draft", "update") or not isinstance(result, str):
        return None
    try:
        decision_id = json.loads(result)["decision"]["id"]
    except (ValueError, TypeError, KeyError):
        return None
    from plans.public import PlanActor, resolve_decision

    db = get_db()
    try:
        actor_row = db.execute(
            "SELECT user_id, project_id FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        if actor_row is None:
            return None
        actor = PlanActor(thread_id, str(actor_row["user_id"]), actor_row["project_id"])
        _, decision, _, _ = resolve_decision(db, decision_id, actor)
        pieces = [decision[key] for key in ("context", "tradeoff", "rationale")]
        for option in decision["options"]:
            pieces.extend((option["label"], option["detail"]))
        if not all(isinstance(piece, str) for piece in pieces):
            return None
        return measure_text("\n".join(pieces))
    finally:
        db.close()
