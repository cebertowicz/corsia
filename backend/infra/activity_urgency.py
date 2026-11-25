"""First-pass order for unranked Activity rows."""


def sort_key(row: dict) -> tuple:
    # Decisions hold plan work; person tasks are due in an agent's plan; a held reply
    # holds an agent's message back until approved. Reviews ask for a look, and
    # document replies are informational.
    level = {'decision': 1, 'task': 1, 'held-reply': 1, 'review': 2, 'reply': 3}.get(
        row.get('renderer') or '', 2,
    )
    return level, row.get('raised_at') or '', row['key']
