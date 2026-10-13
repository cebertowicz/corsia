"""Private assignment writes shared by the two canonical FIFO claim transactions."""
from __future__ import annotations

import json

from infra.error_recovery import stamp_at_turn_start as stamp_recovered_errors
from infra.vendor_login import read_turn_receipt, stamp_initial_claim, without_login_sql


def assign_turn(db, thread_id: str, turn_id: str, rows, *, initial: bool = False):
    """Preserve plan stamps; only an initial claim mints its own login anchor."""
    from core.message_history import PAUSED_SCHEDULE_KEY
    from infra.human_reply import record_human_turn
    from plans.public import thread_paused_since, user_schedule_wakes

    receipt = None if initial else read_turn_receipt(turn_id, db=db)
    message_ids = [str(row['id']) for row in rows]
    placeholders = ','.join('?' for _ in message_ids)
    db.execute(f'UPDATE messages SET metadata={without_login_sql()} WHERE id IN ({placeholders})',
               message_ids)
    since = thread_paused_since(db, thread_id)
    db.execute(
        f"""UPDATE messages SET active_turn_id=?, metadata=CASE WHEN ? IS NOT NULL
            THEN json_set(COALESCE(metadata, '{{}}'), '$.plan_paused_since', ?)
            WHEN json_extract(metadata, '$.plan_paused_since') IS NOT NULL
            THEN json_remove(metadata, '$.plan_paused_since') ELSE metadata END
            WHERE id IN ({placeholders})""",
        (turn_id, since, since, *message_ids),
    )
    # The stop clause is decided here, once, and replays from what is stored.
    marked = set(user_schedule_wakes(db, rows)) if since is not None else set()
    db.execute(
        f"""UPDATE messages SET metadata=CASE WHEN id IN (SELECT value FROM json_each(?))
            THEN json_set(metadata, '$.{PAUSED_SCHEDULE_KEY}', json('true'))
            WHEN json_extract(metadata, '$.{PAUSED_SCHEDULE_KEY}') IS NOT NULL
            THEN json_remove(metadata, '$.{PAUSED_SCHEDULE_KEY}') ELSE metadata END
            WHERE id IN ({placeholders})""",
        (json.dumps(sorted(marked)), *message_ids),
    )
    recovered: tuple[str, ...] = ()
    if initial:
        receipt = stamp_initial_claim(db, turn_id, message_ids[0])
        # The agent moves past any turn-ending error here, in the claim that starts its turn.
        recovered = tuple(stamp_recovered_errors(db, thread_id, rows))
    stamps = record_human_turn(db, thread_id, turn_id, rows, {'plan_paused_since': since})
    return stamps, receipt, recovered
