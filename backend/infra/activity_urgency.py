"""First-pass order for unranked Activity rows."""


def sort_key(row: dict) -> tuple:
    """Urgency level first, then the queue's canonical order (plans/attention_detail.py).

    Decisions hold plan work; person tasks are due in an agent's plan; a held reply
    holds an agent's message back until approved. Reviews ask for a look, and
    document replies are informational. Within a level, ``priority`` keeps held
    replies above decisions and an agent's own ``order_key`` for its decisions;
    rows without one sort as 0, then oldest first.
    """
    level = {'decision': 1, 'task': 1, 'held-reply': 1, 'review': 2, 'reply': 3}.get(
        row.get('renderer') or '', 2,
    )
    return level, row.get('priority', 0), row.get('raised_at') or '', row['key']
