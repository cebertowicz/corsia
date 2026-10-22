"""A stored plan task read as the text prior-word matching uses."""

from __future__ import annotations

from memory.task_match import NodeText


def node_text(db, node_id: str) -> NodeText | None:
    """Title and criteria from the task; intent and quoted words from its canonical intent node.

    A task created without its own intent (follow-ups, revisions, delegated
    worker tasks) points at the node holding it, resolved by Structured Plans;
    a canonical task resolves to itself, the text wake matching scores in
    ``plans.person_input``.
    """
    from plans.public import canonical_intent_node

    row = db.execute(
        "SELECT id,title,intent,acceptance_criteria,intent_node_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    if row is None:
        return None
    source = row if row["intent_node_id"] is None else canonical_intent_node(db, node_id)
    quotes = db.execute(
        "SELECT quote FROM plan_node_intent_words WHERE node_id=? ORDER BY added_at,rowid", (source["id"],),
    ).fetchall()
    return NodeText(
        str(row["title"]), str(row["intent"] or source["intent"] or ""), str(row["acceptance_criteria"] or ""),
        "\n".join(str(quote["quote"]) for quote in quotes), str(row["id"]),
    )
