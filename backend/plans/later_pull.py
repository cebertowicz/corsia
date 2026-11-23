"""The priorities line a plan call adds for each task it took out of Later.

Words only: nothing is refused and nothing is stored. The tool layer reads the
named Later tasks before the call runs and appends one line to the call's
notice for each that left Later.
"""

from __future__ import annotations

import json

from prompts.later_pull import LEFT_LATER


def titles_in_later(db, plan_id: str, params: dict) -> dict[str, str]:
    """The Later tasks this call names (node_id, node_ids, next_node_id), by id, before it runs."""
    many = params.get("node_ids")
    ids = [params.get("node_id"), *(many if isinstance(many, list) else []), params.get("next_node_id")]
    ids = list(dict.fromkeys(value for value in ids if isinstance(value, str) and value))
    if not ids:
        return {}
    # Looked up by id alone: with plan_id in the WHERE, SQLite walks the plan's index
    # and the cost grows with the plan's whole history.
    rows = db.execute(
        "SELECT id, title, plan_id, later FROM plan_nodes WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    ).fetchall()
    titles = {
        str(row["id"]): str(row["title"]) for row in rows if row["plan_id"] == plan_id and row["later"]
    }
    return {node_id: titles[node_id] for node_id in ids if node_id in titles}


def tell_left_later(db, in_later: dict[str, str], result: dict) -> None:
    """Add the priorities line for each named task this call took out of Later."""
    for node_id, title in in_later.items():
        row = db.execute("SELECT later FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
        if row is not None and not row["later"]:
            line = LEFT_LATER.format(title=title)
            existing = result.get("notice")
            result["notice"] = f"{existing}\n{line}" if existing else line
