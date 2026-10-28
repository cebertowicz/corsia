"""Read a person-step card's state from its current node and saved outcome."""

import sqlite3


def person_step_state(row: sqlite3.Row) -> str:
    """Return the current receipt or waiting state for a projected card."""
    state = row["step_node_state"]
    if state in ("accepted", "accepted_with_followup"):
        return "done"
    if state is None:
        return "done" if row["step_outcome"] in ("done", "done_follow_up") else "closed"
    if state == "closed" or row["step_archived_at"] is not None:
        return "closed"
    return "open"
