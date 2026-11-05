"""Which task a plan operation started."""

from __future__ import annotations

#: Plan operations whose result ``node`` is the task they start.
NODE_OPERATIONS = frozenset({"start", "activate", "add", "insert"})


def started_node_id(block: object) -> str | None:
    """Return the selected task when a plan operation actually started it."""
    selected = block.get("selected") if isinstance(block, dict) else None
    if not isinstance(selected, dict) or selected.get("how") not in {"started", "auto_selected"}:
        return None
    return str(selected["id"])
