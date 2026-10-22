"""A stored plan task read as the text prior-word matching uses."""

from __future__ import annotations

from memory.task_match import NodeText


def node_text(db, node_id: str) -> NodeText | None:
    row = db.execute(
        "SELECT id,title,intent,acceptance_criteria FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    if row is None:
        return None
    quotes = db.execute(
        "SELECT quote FROM plan_node_intent_words WHERE node_id=? ORDER BY added_at,rowid", (node_id,),
    ).fetchall()
    return NodeText(
        str(row["title"]), str(row["intent"] or ""), str(row["acceptance_criteria"] or ""),
        "\n".join(str(quote["quote"]) for quote in quotes), str(row["id"]),
    )
