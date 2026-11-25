"""First-pass order for unranked Activity rows."""


def sort_key(row: dict) -> tuple:
    # Decisions hold plan work; person tasks are due in an agent's plan.
    # Held replies ask for approval, and document replies are informational.
    level = {'decision': 1, 'task': 1, 'held-reply': 2, 'review': 2, 'reply': 3}.get(
        row.get('renderer') or '', 2,
    )
    return level, row.get('raised_at') or '', row['key']
