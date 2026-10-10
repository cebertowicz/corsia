"""SQL fragments over a thread update: whether its plan item asks the user, and the ask it recorded.

A ``blocked`` update's source is the ``hold_placed`` event that placed the ask; plans
owns reading it (``plans.user_ask``), so this module names no plan table.
"""

from __future__ import annotations


#: Whether an update's plan item still asks the user, right now. ``asks_user`` is the
#: rule the chat list's Failed mark reads (``failure_asks_user_sql``): a retried,
#: followed-up, deleted, archived or retired-branch item asks nothing.
#: ``blocked_asks_user`` is its twin for an item blocked on the user (``blocked_on_user_sql``);
#: it feeds no mark or dot, only whether the card splits a Focus block.
def asks_user_sql(blocked: bool = False) -> str:
    from plans import public

    rule = (
        public.blocked_on_user_sql('n', public.placed_ask_hold_sql('thread_updates.source_id'))
        if blocked else public.failure_asks_user_sql('n')
    )
    return f"""EXISTS (
    SELECT 1 FROM plan_nodes n
     WHERE n.id = thread_updates.node_id AND {rule}
)"""


def ask_sql(column: str) -> str:
    """One column of the user ask a ``blocked`` update recorded, open or since cleared."""
    from plans import public

    return public.user_ask_hold_sql('thread_updates.source_id', column)
