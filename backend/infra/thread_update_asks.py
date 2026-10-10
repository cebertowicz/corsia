"""SQL fragments over a thread update: whether its plan item asks the user, and the ask it recorded."""

from __future__ import annotations

#: The hold a ``blocked`` update recorded: its source is the ``hold_placed`` event, read
#: by primary key, so no history scan stands behind a card.
_PLACED_HOLD_SQL = (
    "(SELECT json_extract(e.payload_json, '$.hold_id') FROM plan_node_events e"
    " WHERE e.id = thread_updates.source_id)"
)

#: Whether an update's plan item still asks the user, right now. ``asks_user`` is the
#: rule the chat list's Failed mark reads (``failure_asks_user_sql``): a retried,
#: followed-up, deleted, archived or retired-branch item asks nothing.
#: ``blocked_asks_user`` is its twin for an item blocked on the user (``blocked_on_user_sql``);
#: it feeds no mark or dot, only whether the card splits a Focus block.
def asks_user_sql(blocked: bool = False) -> str:
    from plans import public

    rule = (
        public.blocked_on_user_sql('n', _PLACED_HOLD_SQL) if blocked
        else public.failure_asks_user_sql('n')
    )
    return f"""EXISTS (
    SELECT 1 FROM plan_nodes n
     WHERE n.id = thread_updates.node_id AND {rule}
)"""


def ask_sql(column: str) -> str:
    """One column of the user ask a ``blocked`` update recorded, open or since cleared."""
    return f"""(SELECT h.{column} FROM plan_node_holds h
            WHERE h.id = {_PLACED_HOLD_SQL} AND h.kind='blocked'
              AND h.owner_kind='user' AND h.owner_id IS NOT NULL)"""
